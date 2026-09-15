import signal
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.commands import run_daemon
from wb.cloud_agent.handlers.curl import CloudNetworkError
from wb.cloud_agent.main import main
from wb.cloud_agent.mqtt import MQTTCloudAgent


def test_broker_recovery_restores_the_latest_retained_snapshot(settings):
    """
    After broker state loss, restore metadata, current values and the provider list.
    """
    with patch("wb.cloud_agent.mqtt.MQTTClient") as factory:
        agent = MQTTCloudAgent(settings)
        client = factory.return_value
        client.is_connected.return_value = False
        agent.publish_vdev()
        agent.publish_ctrl("status", "starting")
        agent.publish_ctrl("status", "ok")
        agent.publish_providers("one,two")
        client.publish.assert_not_called()
        client.is_connected.return_value = True
        client.on_connect(client, None, None, 0)
        published = {args[0]: args[1] for args, _kwargs in client.publish.call_args_list}
        assert published[f"{settings.mqtt_prefix}/meta/driver"] == "wb-cloud-agent"
        assert published[f"{settings.mqtt_prefix}/controls/status"] == "ok"
        assert published["/wb-cloud-agent/providers"] == "one,two"


@pytest.mark.parametrize("reason", [4, 5])
def test_initial_authentication_refusal_is_terminal(settings, reason):
    """
    A refused initial CONNACK reports authentication failure to the caller.
    """
    with patch("wb.cloud_agent.mqtt.MQTTClient") as factory:
        agent = MQTTCloudAgent(settings)
        client = factory.return_value
        client.start.side_effect = lambda: client.on_connect(client, None, None, reason)
        with pytest.raises(PermissionError):
            agent.connect(threading.Event())


def test_reconnect_after_cleanup_cannot_restore_deleted_topics(settings):
    """
    Cleanup is final, including when a late CONNACK races with shutdown.
    """
    with patch("wb.cloud_agent.mqtt.MQTTClient") as factory:
        agent = MQTTCloudAgent(settings)
        client = factory.return_value
        agent.publish_ctrl("status", "ok")
        client.on_connect(client, None, None, 0)
        client.on_disconnect(client, None, 1)
        agent.remove_vdev()
        client.publish.reset_mock()
        client.on_connect(client, None, None, 0)
        client.publish.assert_not_called()


def test_hardware_revision_is_received_without_a_cloud_request(settings):
    """
    Receiving retained controller information must never call curl in the MQTT callback.
    """
    with patch("wb.cloud_agent.mqtt.MQTTClient") as factory:
        agent = MQTTCloudAgent(settings)
        client = factory.return_value
        client.on_message(client, None, SimpleNamespace(payload=b"WB8-test"))
        assert agent.hardware_revision == "WB8-test"


def test_shutdown_disconnects_before_stopping_the_mqtt_loop(settings):
    """
    Normal shutdown must not trigger the broker's unexpected-disconnect Last Will.
    """
    with patch("wb.cloud_agent.mqtt.MQTTClient") as factory:
        agent = MQTTCloudAgent(settings)
        calls = MagicMock()
        calls.attach_mock(factory.return_value.disconnect, "disconnect")
        calls.attach_mock(factory.return_value.loop_stop, "loop_stop")
        agent.stop()
        assert [call[0] for call in calls.mock_calls] == ["disconnect", "loop_stop"]


@pytest.fixture(name="daemon")
def fixture_daemon(settings, monkeypatch):
    settings.request_period_seconds = 0
    settings.ping_period_seconds = 0
    handlers = {}
    monkeypatch.setattr("signal.signal", handlers.__setitem__)
    transport = MagicMock()
    transport.start.side_effect = lambda: transport.on_connect(transport, None, None, 0)
    monkeypatch.setattr("wb.cloud_agent.mqtt.MQTTClient", lambda *_args: transport)
    monkeypatch.setattr("wb.cloud_agent.commands.configure_app", lambda **_kwargs: settings)
    monkeypatch.setattr("wb.cloud_agent.commands.read_activation_link", lambda _settings: "saved-link")
    monkeypatch.setattr("wb.cloud_agent.mqtt.get_provider_names", lambda: ["test"])
    monkeypatch.setattr("requests.head", lambda *_args, **_kwargs: SimpleNamespace(status_code=200))
    monkeypatch.setattr("wb.cloud_agent.commands.reconcile_metrics_script", lambda _settings: None)
    startup, packages, event = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr("wb.cloud_agent.commands.make_start_up_request", startup)
    monkeypatch.setattr("wb.cloud_agent.commands.send_packages_version", packages)
    monkeypatch.setattr("wb.cloud_agent.commands.make_event_request", event)

    def stop(signum=signal.SIGTERM):
        handlers[signum](signum, None)

    event.side_effect = lambda *_args: stop()
    return SimpleNamespace(
        settings=settings,
        transport=transport,
        startup=startup,
        packages=packages,
        event=event,
        stop=stop,
        options=SimpleNamespace(provider_name="test", config=None, broker=None),
    )


def test_startup_retries_without_changing_starting_status(daemon):
    """
    A failed startup is retried; connecting is not published until startup and versions complete.
    """
    statuses = []
    published = daemon.transport.publish.return_value

    def publish(topic, value, **_kwargs):
        if topic.endswith("/controls/status"):
            statuses.append(value)
        return published

    def startup(*_args):
        assert statuses[-1] == "starting"
        if daemon.startup.call_count == 1:
            raise CloudNetworkError("offline")

    def packages(*_args):
        assert statuses[-1] == "starting"

    daemon.transport.publish.side_effect = publish
    daemon.startup.side_effect = startup
    daemon.packages.side_effect = packages
    assert run_daemon(daemon.options) == 0
    assert daemon.startup.call_count == 2
    assert statuses[:3] == ["starting", "connecting", "ok"]
    assert statuses[-1] == ""


@pytest.mark.parametrize("failure", [CloudNetworkError("offline"), subprocess.TimeoutExpired("curl", 360)])
def test_event_failure_recovers_in_the_same_daemon(daemon, failure):
    def event(*_args):
        if daemon.event.call_count == 1:
            raise failure
        daemon.stop()

    daemon.event.side_effect = event
    assert run_daemon(daemon.options) == 0
    assert daemon.event.call_count == 2
    assert daemon.startup.call_count == 1
    daemon.transport.publish.assert_any_call(
        f"{daemon.settings.mqtt_prefix}/controls/status", "ok", retain=True, qos=1
    )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_stop_while_cloud_unavailable_preserves_saved_link_then_cleans_up(daemon, monkeypatch, signum):
    def head(*_args, **_kwargs):
        daemon.transport.publish.assert_any_call(
            f"{daemon.settings.mqtt_prefix}/controls/activation_link", "saved-link", retain=True, qos=1
        )
        daemon.stop(signum)
        return SimpleNamespace(status_code=503)

    monkeypatch.setattr("requests.head", head)
    assert run_daemon(daemon.options) == 0
    daemon.startup.assert_not_called()
    daemon.transport.publish.assert_any_call(
        f"{daemon.settings.mqtt_prefix}/controls/status", "", retain=True, qos=1
    )
    daemon.transport.disconnect.assert_called_once()
    daemon.transport.loop_stop.assert_called_once()


def test_daemon_accepts_custom_broker(daemon):
    daemon.options.broker = "tcp://127.0.0.1:1884"
    assert run_daemon(daemon.options) == 0
    assert daemon.settings.broker_url == daemon.options.broker


@pytest.mark.parametrize("reason", [3, 4, 5])
def test_authentication_refusal_after_connection_is_not_terminal(daemon, reason):
    def event(*_args):
        client = daemon.transport
        client.on_connect(client, None, None, reason)
        client.on_connect(client, None, None, 0)
        daemon.stop()

    daemon.event.side_effect = event
    assert run_daemon(daemon.options) == 0


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_stop_during_initial_broker_outage(daemon, signum, caplog):
    def start():
        daemon.stop(signum)
        raise ConnectionRefusedError("offline")

    daemon.transport.start.side_effect = start
    daemon.transport.is_connected.return_value = False
    assert run_daemon(daemon.options) == 0
    daemon.startup.assert_not_called()
    assert "Cannot remove cloud agent MQTT topics" in caplog.text
    daemon.transport.disconnect.assert_called_once()


@pytest.mark.parametrize("contents", [None, "{broken", "[]"])
def test_invalid_configuration_exits_six(tmp_path, monkeypatch, contents):
    config = tmp_path / "provider.conf"
    if contents is not None:
        config.write_text(contents)
    monkeypatch.setattr("sys.argv", ["wb-cloud-agent", "run-daemon", "test", "-c", str(config)])
    assert main() == 6


def test_initial_broker_failure_is_retried(settings):
    """
    A missing socket on the first attempt does not require a new process or client object.
    """
    stop = threading.Event()
    with patch("wb.cloud_agent.mqtt.MQTTClient") as factory, patch.object(stop, "wait"):
        agent = MQTTCloudAgent(settings)
        client = factory.return_value

        def start():
            if client.start.call_count == 1:
                raise ConnectionRefusedError("offline")
            client.on_connect(client, None, None, 0)

        client.start.side_effect = start
        assert agent.connect(stop)
        assert client.start.call_count == 2
