import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_services import CLOUD_VARS
from wb.cloud_agent.services import metrics


@pytest.fixture(name="collector")
def collector_module(tmp_path, cloud_vars_settings, monkeypatch):
    """
    The packaged collector template, rendered like on a controller and imported as a module.

    MQTT transport is replaced by a mock, the MQTT-RPC client is real.
    """
    # pybuild runs the suite from its build tree, the template stays in the source root above it
    template = next(
        parent / "metrics_collector.py.tpl"
        for parent in Path(__file__).resolve().parents
        if (parent / "metrics_collector.py.tpl").is_file()
    )
    monkeypatch.setattr(metrics, "METRICS_COLLECTOR_TEMPLATE_PATH", str(template))
    source = metrics.render_metrics_script(
        cloud_vars_settings, {"vars": CLOUD_VARS, "mqtt_client_id": "collector-test", "created_at": "test"}
    )
    path = tmp_path / "metrics_collector.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("metrics_collector_under_test", path)
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


@pytest.mark.parametrize("login_rejected, exit_code", [(True, 2), (False, 0)])
def test_run_forever_exit_code_without_connection(collector, monkeypatch, login_rejected, exit_code):
    def fail_start(self):
        self.login_rejected = login_rejected
        return False

    monkeypatch.setattr(collector.MQTTConnection, "start", fail_start)

    assert collector.run_forever() == exit_code


def test_connecting_log_line_keeps_the_credentials_out_of_the_journal(collector, monkeypatch, caplog):
    monkeypatch.setattr(collector, "BROKER_URL", "tcp://user:s3cr3t@localhost:1883")
    connection = collector.MQTTConnection()
    collector.STOP_REQUESTED.set()

    with caplog.at_level(logging.INFO):
        connection.start()

    assert "Connecting to MQTT broker tcp://localhost:1883" in caplog.text
    assert "s3cr3t" not in caplog.text
