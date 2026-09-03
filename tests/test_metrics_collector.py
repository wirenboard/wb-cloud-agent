# pylint: disable=exec-used,no-member,protected-access,redefined-outer-name

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests import CLOUD_VARS
from wb.cloud_agent.services.metrics import render_metrics_script


def _find_source_file(relative_path: str) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative_path
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Source file not found: {relative_path}")


@pytest.fixture
def collector(cloud_vars_settings):  # pylint: disable=redefined-outer-name
    template_path = _find_source_file("metrics_collector.py.tpl")
    conf = {
        "vars": CLOUD_VARS,
        "mqtt_client_id": "wb-cloud-agent-metrics-test",
        "created_at": "2026-09-02 00:00:00+00:00",
    }
    with patch("wb.cloud_agent.services.metrics.METRICS_COLLECTOR_TEMPLATE_PATH", str(template_path)):
        rendered = render_metrics_script(cloud_vars_settings, conf)

    module = types.ModuleType("metrics_collector")
    exec(compile(rendered, "metrics_collector.py", "exec"), module.__dict__)  # noqa: S102
    yield module
    module._STOP_REQUESTED.clear()


@pytest.mark.parametrize(
    "reason_code",
    [4, 5, 134, 135],
)
def test_mqtt_auth_connack_returns_2(collector, reason_code):
    state = collector.MQTTConnectionState()

    state.on_connect(None, None, None, reason_code)

    assert state.wait_for_connection() == 2


@pytest.mark.parametrize("reason_code", [1, 2, 3])
def test_other_mqtt_connack_is_retried(collector, reason_code):
    state = collector.MQTTConnectionState()

    state.on_connect(None, None, None, reason_code)

    assert state.requested_exit_code() is None

    state.on_connect(None, None, None, 0)

    assert state.wait_for_connection() is None
    assert state.connection_generation() == 1


def test_connect_mqtt_waits_for_successful_connack(collector):
    client = MagicMock()
    client.start.side_effect = lambda: client.on_connect(None, None, None, 0)
    collector.MQTTClient = MagicMock(return_value=client)

    connected_client, state = collector.connect_mqtt()

    assert connected_client is client
    assert state.wait_for_connection() is None
    client.stop.assert_not_called()


def test_connect_mqtt_maps_auth_failure_to_exit_2(collector):
    client = MagicMock()
    client.start.side_effect = lambda: client.on_connect(None, None, None, 5)
    collector.MQTTClient = MagicMock(return_value=client)

    with pytest.raises(collector.CollectorExit) as exc_info:
        collector.connect_mqtt()

    assert exc_info.value.exit_code == 2
    client.stop.assert_called_once_with()


def test_rpc_client_is_recreated_after_mqtt_reconnect(collector):
    client = MagicMock()
    old_rpc = MagicMock()
    new_rpc = MagicMock()
    state = collector.MQTTConnectionState()
    state.on_connect(None, None, None, 0)
    initial_generation = state.connection_generation()

    unchanged_rpc, unchanged_generation = collector.refresh_rpc_client_after_reconnect(
        client, old_rpc, state, initial_generation
    )

    assert unchanged_rpc is old_rpc
    assert unchanged_generation == initial_generation

    state.on_disconnect(None, None, 1)
    state.on_connect(None, None, None, 0)
    with patch.object(collector, "create_rpc_client", return_value=new_rpc) as create_rpc_client:
        refreshed_rpc, refreshed_generation = collector.refresh_rpc_client_after_reconnect(
            client, old_rpc, state, initial_generation
        )

    assert refreshed_rpc is new_rpc
    assert refreshed_generation == initial_generation + 1
    create_rpc_client.assert_called_once_with(client)


def test_unavailable_broker_wait_is_stoppable(collector, caplog):
    collector.BROKER_URL = "tcp://user:secret@127.0.0.1:18889"
    client = MagicMock()

    def report_failure_and_stop():
        client.on_connect_fail(None, None)
        collector._STOP_REQUESTED.set()

    client.start.side_effect = report_failure_and_stop
    collector.MQTTClient = MagicMock(return_value=client)

    with pytest.raises(collector.CollectorExit) as exc_info:
        collector.connect_mqtt()

    assert exc_info.value.exit_code == 7
    assert "unavailable" in caplog.text


def test_run_forever_stops_client_and_returns_7(collector):
    client = MagicMock()
    client.is_connected.return_value = True
    state = collector.MQTTConnectionState()
    collector._STOP_REQUESTED.set()

    with (
        patch.object(collector, "connect_mqtt_rpc", return_value=(client, MagicMock(), state)),
        patch.object(collector, "collect_once") as collect_once,
    ):
        exit_code = collector.run_forever()

    assert exit_code == 7
    collect_once.assert_not_called()
    client.stop.assert_called_once_with()


def test_unknown_argument_exits_with_2(collector):
    with pytest.raises(SystemExit) as exc_info:
        collector.main(["--unknown"])

    assert exc_info.value.code == 2


def test_metrics_unit_uses_controller_exit_policy():
    unit = _find_source_file("debian/wb-cloud-agent.wb-cloud-agent-metrics@.service").read_text(
        encoding="utf-8"
    )

    assert "Restart=on-failure" in unit
    assert "RestartPreventExitStatus=2 6" in unit
    assert "SuccessExitStatus=7" in unit
