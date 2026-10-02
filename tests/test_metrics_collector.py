import importlib.util
import json
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_services import CLOUD_VARS
from wb.cloud_agent.services import metrics


@pytest.fixture(name="collector_script", scope="session")
def rendered_collector_script(tmp_path_factory):
    """
    The packaged collector template rendered like on a controller, once per session: coverage
    counts the file by path, a copy per test would be measured as a separate file each.
    """
    # pybuild runs the suite from its build tree, the template stays in the source root above it
    template = next(
        parent / "metrics_collector.py.tpl"
        for parent in Path(__file__).resolve().parents
        if (parent / "metrics_collector.py.tpl").is_file()
    )
    state_dir = tmp_path_factory.mktemp("collector")
    settings = SimpleNamespace(
        broker_url="tcp://localhost:1883",
        client_cert_engine_key="ATECCx08:00:02:C0:00",
        metrics_last_uid=state_dir / "metrics_last_uid",
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(metrics, "METRICS_COLLECTOR_TEMPLATE_PATH", str(template))
        source = metrics.render_metrics_script(
            settings, {"vars": CLOUD_VARS, "mqtt_client_id": "collector-test", "created_at": "test"}
        )
    path = state_dir / "metrics_collector.py"
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture(name="collector")
def collector_module(collector_script, monkeypatch):
    """
    The rendered collector imported as a fresh module for the test.

    MQTT transport is replaced by a mock, the MQTT-RPC client is real.
    """
    spec = importlib.util.spec_from_file_location("metrics_collector_under_test", collector_script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    mqtt_client = MagicMock(_client_id=b"collector-test")
    monkeypatch.setattr(module, "MQTTClient", MagicMock(return_value=mqtt_client))
    monkeypatch.setattr(module, "CONNACK_POLL_INTERVAL_SECONDS", 0.01)
    module.STOP_REQUESTED.clear()
    return module


def test_rpc_gets_replies_after_broker_reconnect(collector):
    """
    A new broker session has no subscriptions, so the RPC client must subscribe to the replies again.
    """
    connection = collector.MQTTConnection()
    client = connection.client
    subscribed = set()
    client.subscribe.side_effect = subscribed.add

    def broker(topic, payload):
        # answers a request only when the client is subscribed to the reply topic
        if topic + "/reply" in subscribed:
            request = json.loads(payload)
            reply = json.dumps({"id": request["id"], "result": request["params"]}).encode()
            client.on_message(client, None, SimpleNamespace(topic=topic + "/reply", payload=reply))

    client.publish.side_effect = broker

    for session in range(3):
        subscribed.clear()  # the broker restarted: the session and its subscriptions are gone
        client.on_connect(client, None, {}, 0)
        result = connection.rpc.call("db_logger", "history", "get_values", {"session": session}, timeout=0)
        assert result == {"session": session}


def test_rejected_login_stops_startup(collector):
    connection = collector.MQTTConnection()
    connection.client.on_connect(connection.client, None, {}, 5)

    assert connection.start() is False

    assert connection.login_rejected is True
    connection.client.start.assert_called_once_with(retry_first_connection=True)


def test_rejected_login_after_reconnect_stops_the_loop(collector):
    connection = collector.MQTTConnection()
    connection.client.on_connect(connection.client, None, {}, 0)

    connection.client.on_connect(connection.client, None, {}, 5)

    assert connection.login_rejected is True
    assert collector.STOP_REQUESTED.is_set()


def test_stop_request_ends_waiting_for_broker(collector):
    connection = collector.MQTTConnection()
    collector.STOP_REQUESTED.set()

    assert connection.start() is False

    assert connection.login_rejected is False


def test_stop_during_a_catch_up_fetch_abandons_the_fetch(collector, monkeypatch):
    """
    A stop during the pause between two wb-mqtt-db calls ends the fetch at once, and nothing of it
    is returned: a partial result would be sent and last_uid would skip the channels left unread.
    """
    monkeypatch.setattr(collector, "CHANNEL_BATCH_SIZE", 1)
    rpc_calls = []
    monkeypatch.setattr(
        collector, "_call_history_get_values", lambda _rpc, params: rpc_calls.append(params) or {}
    )
    collector.STOP_REQUESTED.set()

    with pytest.raises(collector.StopRequested):
        collector.get_values(None, [{"pair": "a"}, {"pair": "b"}], 0, 10, inter_batch_sleep=60)

    assert len(rpc_calls) == 1


def test_stop_while_rate_limited_does_not_wait_for_the_retry(collector, monkeypatch):
    """
    The retry delay after HTTP 429 is a setting and can be long; a stop ends it. The batch is not
    sent, so last_uid stays where it was and the batch goes out at the next start.
    """
    monkeypatch.setattr(collector, "SEND_MAX_RETRIES", 3)
    monkeypatch.setattr(collector, "SEND_RATE_LIMIT_RETRY_DELAY_SECONDS", 60)
    curl_calls = []
    monkeypatch.setattr(collector, "_run_curl", lambda command, payload: curl_calls.append(command) or "429")
    collector.STOP_REQUESTED.set()

    with pytest.raises(collector.StopRequested):
        collector.send_lines(["m value=1 1"])

    assert len(curl_calls) == 1


def test_run_forever_ends_on_a_stop_inside_the_iteration(collector, monkeypatch):
    iterations = []

    def collect_once(_rpc, _client, _catch_up):
        iterations.append(1)
        raise collector.StopRequested()

    monkeypatch.setattr(collector.MQTTConnection, "start", lambda self: True)
    monkeypatch.setattr(collector, "collect_once", collect_once)

    assert collector.run_forever() == collector.EXIT_SUCCESS

    assert iterations == [1]


@pytest.mark.parametrize("login_rejected, exit_code", [(True, 2), (False, 0)])
def test_run_forever_exit_code_without_connection(collector, monkeypatch, login_rejected, exit_code):
    def fail_start(self):
        self.login_rejected = login_rejected
        return False

    monkeypatch.setattr(collector.MQTTConnection, "start", fail_start)

    assert collector.run_forever() == exit_code


def _connected(collector, monkeypatch):
    """MQTTConnection.start() succeeds at once; the connections made are collected for the test."""
    connections = []

    def start(self):
        connections.append(self)
        return True

    monkeypatch.setattr(collector.MQTTConnection, "start", start)
    monkeypatch.setattr(collector, "INTERVAL_SECONDS", 0)
    return connections


def test_run_forever_stops_between_iterations_with_0(collector, monkeypatch):
    """
    A stop requested while an iteration runs, as the signal handler does: the loop ends after that
    iteration, the MQTT client is stopped and the exit code is 0.
    """
    _connected(collector, monkeypatch)
    iterations = []

    def collect_once(_rpc, _client, _catch_up):
        iterations.append(1)
        collector.STOP_REQUESTED.set()
        return False

    monkeypatch.setattr(collector, "collect_once", collect_once)

    assert collector.run_forever() == collector.EXIT_SUCCESS

    assert iterations == [1]
    collector.MQTTClient.return_value.stop.assert_called_once_with()


def test_run_forever_exits_2_when_the_broker_rejects_the_login_after_a_reconnect(collector, monkeypatch):
    """
    The broker came back with another password file: the reconnect gets CONNACK 5 on paho's thread
    while an iteration runs. The loop ends after that iteration and the exit code is 2.
    """
    connections = _connected(collector, monkeypatch)

    def collect_once(_rpc, client, _catch_up):
        client.on_connect(client, None, {}, 5)
        return False

    monkeypatch.setattr(collector, "collect_once", collect_once)

    assert collector.run_forever() == collector.EXIT_INVALIDARGUMENT

    assert connections[0].login_rejected is True


def test_rpc_timeout_with_wb_mqtt_db_down_pauses_the_rpc_until_it_is_back(collector, monkeypatch):
    """
    A wb-mqtt-db RPC timeout while systemd reports the service down: no RPC is attempted until the
    service is active again, only systemd is probed once per cycle. Then the collection resumes.
    """
    _connected(collector, monkeypatch)
    states = iter(["failed", "failed", "active"])
    monkeypatch.setattr(collector, "get_service_state", lambda _name: next(states))
    calls = []

    def collect_once(_rpc, _client, _catch_up):
        calls.append(1)
        if len(calls) == 1:
            raise collector.MQTTRPCTimeoutError()
        collector.STOP_REQUESTED.set()
        return False

    monkeypatch.setattr(collector, "collect_once", collect_once)

    assert collector.run_forever() == collector.EXIT_SUCCESS

    assert calls == [1, 1]
    with pytest.raises(StopIteration):  # one probe in the timeout handler, two while waiting
        next(states)


def test_main_installs_the_signal_handlers_and_returns_the_loop_exit_code(collector, monkeypatch):
    """SIGTERM and SIGINT request the stop; main() returns whatever the loop returns."""
    handlers = {}
    monkeypatch.setattr(collector.signal, "signal", handlers.__setitem__)

    def run_forever():
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 7

    monkeypatch.setattr(collector, "run_forever", run_forever)

    assert collector.main() == 7

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    assert collector.STOP_REQUESTED.is_set()
