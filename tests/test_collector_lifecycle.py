from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from string import Template
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(name="collector")
def fixture_collector(tmp_path):
    template_path = next(
        parent / "metrics_collector.py.tpl"
        for parent in Path(__file__).resolve().parents
        if (parent / "metrics_collector.py.tpl").is_file()
    )
    template = Template(template_path.read_text())
    script = tmp_path / "collector.py"
    script.write_text(template.substitute(dict.fromkeys(template.get_identifiers(), "1")))
    spec = spec_from_file_location("collector_lifecycle", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("reason", [4, 5])
def test_initial_authentication_failure_exits_two(collector, reason):
    with patch.object(collector, "MQTTClient") as factory:
        client = factory.return_value
        client.start.side_effect = lambda: client.on_connect(client, None, None, reason)
        with pytest.raises(SystemExit) as result:
            collector.connect_mqtt()
        assert result.value.code == 2
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()


def test_stop_without_connack_exits_zero(collector):
    with patch.object(collector, "MQTTClient") as factory:
        factory.return_value.start.side_effect = collector.STOP_REQUESTED.set
        with pytest.raises(SystemExit) as result:
            collector.connect_mqtt()
        assert result.value.code == 0
        factory.return_value.disconnect.assert_called_once()


def test_authentication_refusal_after_success_is_retried(collector):
    """
    Recreating an MQTT client after a successful session must not reinstate fatal authentication handling.
    """
    with patch.object(collector, "MQTTClient") as factory:
        client = factory.return_value
        client.start.side_effect = lambda: client.on_connect(client, None, None, 0)
        assert collector.connect_mqtt() is client

        def reconnect():
            client.on_connect(client, None, None, 5)
            client.on_connect(client, None, None, 0)

        client.start.side_effect = reconnect
        assert collector.connect_mqtt() is client
        client.disconnect.assert_not_called()


def test_reconnect_invalidates_rpc_reply_subscriptions(collector):
    client = MagicMock()
    with patch.object(collector, "TMQTTRPCClient") as factory:
        factory.return_value.subscribes = {"/rpc/reply"}
        rpc = collector.create_rpc_client(client)
        client.on_connect(client, None, None, 0)
        assert rpc.subscribes == set()


def test_stop_during_rate_limit_retry_preserves_confirmed_checkpoint(collector):
    """
    Batch one succeeds, batch two gets HTTP 429; stopping must not acknowledge unsent metrics locally.
    """
    collector.SEND_BATCH_SIZE = 1
    collector.SEND_MAX_RETRIES = 3
    collector.MAX_REQUEST_BYTES = 10000
    with (
        patch.object(collector, "load_last_uid", return_value=1),
        patch.object(collector, "get_cached_channels", return_value=[{"pair": ["test", "value"]}]),
        patch.object(collector, "_maybe_refresh_static_lines", return_value=([], False)),
        patch.object(collector, "_fetch_new_values", return_value=([{"uid": 10}, {"uid": 20}], False)),
        patch.object(collector, "value_to_influx_line", return_value="value=1"),
        patch.object(collector, "_run_curl", side_effect=["204", "429"]),
        patch.object(collector.STOP_REQUESTED, "wait", return_value=True),
        patch.object(collector, "save_last_uid") as save,
        pytest.raises(SystemExit) as result,
    ):
        collector.collect_once(MagicMock(), MagicMock(), False)
    assert result.value.code == 0
    save.assert_called_once_with(10)


def test_stop_interrupts_iteration_delay_and_disconnects(collector):
    """
    Finishing one iteration after a stop request must not start the next one.
    """
    client = MagicMock()
    with (
        patch.object(collector, "connect_mqtt_rpc", return_value=(client, MagicMock())),
        patch.object(
            collector, "collect_once", side_effect=lambda *_args: collector.STOP_REQUESTED.set()
        ) as collect,
        pytest.raises(SystemExit) as result,
    ):
        collector.run_forever()
    assert result.value.code == 0
    collect.assert_called_once()
    client.disconnect.assert_called_once()
    client.loop_stop.assert_called_once()
