# pylint: disable=redefined-outer-name

import signal
import subprocess
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.commands import (
    add_on_premise_provider,
    add_provider,
    del_all_providers,
    del_controller_from_cloud,
    del_provider,
    run_daemon,
    show_providers,
)
from wb.cloud_agent.handlers.curl import CloudNetworkError


@pytest.fixture
def mock_mqtt_cloud_agent():
    with patch("wb.cloud_agent.commands.MQTTCloudAgent") as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock_instance


def test_show_providers_empty():
    options = Namespace()

    with (
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("wb.cloud_agent.commands.load_providers_data", return_value=[]),
        patch("wb.cloud_agent.commands.show_providers_table") as mock_show,
    ):
        result = show_providers(options)

        assert result == 0
        mock_show.assert_called_once_with([])


def test_show_providers_with_data():
    options = Namespace()
    providers = [MagicMock(name="provider1"), MagicMock(name="provider2")]

    with (
        patch(
            "wb.cloud_agent.commands.get_provider_names",
            return_value=["provider1", "provider2"],
        ),
        patch("wb.cloud_agent.commands.load_providers_data", return_value=providers),
        patch("wb.cloud_agent.commands.show_providers_table") as mock_show,
    ):
        result = show_providers(options)

        assert result == 0
        mock_show.assert_called_once_with(providers)


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_add_provider_success(mock_mqtt_cloud_agent):
    options = Namespace(base_url="https://example.com/", name=None)

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("wb.cloud_agent.commands.generate_provider_config") as mock_gen,
        patch("wb.cloud_agent.commands.start_and_enable_service") as mock_service,
        patch("builtins.print") as mock_print,
    ):
        mock_settings = MagicMock()
        mock_config.return_value = mock_settings

        result = add_provider(options)

        assert result == 0
        mock_gen.assert_called_once_with("example.com", "https://example.com")
        mock_service.assert_called_once_with("wb-cloud-agent@example.com.service")
        mock_mqtt_cloud_agent.start.assert_called_once()
        mock_mqtt_cloud_agent.update_providers_list.assert_called_once()
        mock_print.assert_called_once_with("Provider example.com successfully added")


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_add_provider_with_custom_name():
    options = Namespace(base_url="https://example.com/", name="custom_name:-123.")

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("wb.cloud_agent.commands.generate_provider_config") as mock_gen,
        patch("wb.cloud_agent.commands.start_and_enable_service"),
        patch("builtins.print"),
    ):
        mock_settings = MagicMock()
        mock_config.return_value = mock_settings

        result = add_provider(options)

        assert result == 0
        mock_gen.assert_called_once_with("custom_name:-123.", "https://example.com")


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_add_provider_already_exists():
    options = Namespace(base_url="https://example.com", name=None)

    with (
        patch("wb.cloud_agent.commands.configure_app"),
        patch("wb.cloud_agent.commands.get_provider_names", return_value=["example.com"]),
        patch("builtins.print") as mock_print,
    ):
        result = add_provider(options)

        assert result == 1
        mock_print.assert_called_once_with("Provider example.com already exists")


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_add_provider_with_duplicate_url():
    options = Namespace(base_url="https://example.com/", name="custom_name")
    existing_provider = MagicMock()
    existing_provider.config = {"CLOUD_BASE_URL": "https://example.com"}

    with (
        patch("wb.cloud_agent.commands.configure_app"),
        patch("wb.cloud_agent.commands.get_provider_names", return_value=["example.com"]),
        patch("wb.cloud_agent.commands.load_providers_data", return_value=[existing_provider]),
        patch("wb.cloud_agent.commands.generate_provider_config") as mock_gen,
        patch("wb.cloud_agent.commands.start_and_enable_service") as mock_service,
        patch("builtins.print") as mock_print,
    ):
        result = add_provider(options)

        assert result == 1
        mock_gen.assert_not_called()
        mock_service.assert_not_called()
        mock_print.assert_called_once_with("Provider with URL https://example.com already exists")


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_add_provider_mqtt_connection_error(mock_mqtt_cloud_agent):
    options = Namespace(base_url="https://example.com", name=None)
    mock_mqtt_cloud_agent.start.side_effect = ConnectionError("Connection failed")

    with (
        patch("wb.cloud_agent.commands.configure_app"),
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("wb.cloud_agent.commands.generate_provider_config"),
        patch("wb.cloud_agent.commands.start_and_enable_service"),
        patch("builtins.print"),
    ):
        result = add_provider(options)

        assert result == 0  # Still succeeds even if MQTT fails


def test_add_provider_mqtt_update_error(mock_mqtt_cloud_agent):
    options = Namespace(base_url="https://example.com", name=None)
    mock_mqtt_cloud_agent.update_providers_list.side_effect = ConnectionError("Update failed")

    with (
        patch("wb.cloud_agent.commands.configure_app"),
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("wb.cloud_agent.commands.generate_provider_config"),
        patch("wb.cloud_agent.commands.start_and_enable_service"),
        patch("builtins.print"),
    ):
        result = add_provider(options)

        assert result == 0  # Still succeeds even if MQTT update fails


def test_add_on_premise_provider():
    options = Namespace(base_url="https://on-premise.com", name=None)

    with (
        patch("wb.cloud_agent.commands.del_all_providers") as mock_del,
        patch("wb.cloud_agent.commands.add_provider", return_value=0) as mock_add,
    ):
        result = add_on_premise_provider(options)

        assert result == 0
        mock_del.assert_called_once_with(options, show_msg=False)
        mock_add.assert_called_once_with(options)


def test_del_provider_success(mock_mqtt_cloud_agent):
    options = Namespace(provider_name="test_provider")

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.get_provider_names", return_value=["test_provider"]),
        patch("wb.cloud_agent.commands.stop_services_and_del_configs") as mock_stop,
    ):
        mock_settings = MagicMock()
        mock_config.return_value = mock_settings

        result = del_provider(options)

        assert result == 0
        mock_stop.assert_called_once_with(mock_settings, "test_provider")
        mock_mqtt_cloud_agent.update_providers_list.assert_called_once()


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_del_provider_not_exists():
    options = Namespace(provider_name="nonexistent")

    with (
        patch("wb.cloud_agent.commands.configure_app"),
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("builtins.print") as mock_print,
    ):
        result = del_provider(options)

        assert result == 1
        mock_print.assert_called_once_with("Provider nonexistent does not exists")


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_del_provider_with_url_format():
    options = Namespace(provider_name="https://example.com")

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.get_provider_names", return_value=["example.com"]),
        patch("wb.cloud_agent.commands.stop_services_and_del_configs"),
    ):
        mock_settings = MagicMock()
        mock_config.return_value = mock_settings

        result = del_provider(options)

        assert result == 0
        mock_config.assert_called_once_with(provider_name="example.com")


def test_del_all_providers_empty():
    options = Namespace()

    with (
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("builtins.print") as mock_print,
    ):
        result = del_all_providers(options)

        assert result == 1
        mock_print.assert_called_once_with("No one provider was found")


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_del_all_providers_success():
    options = Namespace()

    with (
        patch(
            "wb.cloud_agent.commands.get_provider_names",
            return_value=["provider1", "provider2"],
        ),
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.stop_services_and_del_configs") as mock_stop,
    ):
        mock_settings = MagicMock()
        mock_config.return_value = mock_settings

        result = del_all_providers(options)

        assert result == 0
        assert mock_config.call_count == 2
        assert mock_stop.call_count == 2


def test_del_all_providers_no_message():
    options = Namespace()

    with (
        patch("wb.cloud_agent.commands.get_provider_names", return_value=[]),
        patch("builtins.print") as mock_print,
    ):
        result = del_all_providers(options, show_msg=False)

        assert result == 1
        mock_print.assert_not_called()


def test_del_controller_from_cloud_success():
    options = Namespace(base_url="https://example.com")

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.event_delete_controller", return_value=0) as mock_delete,
    ):
        mock_settings = MagicMock()
        mock_config.return_value = mock_settings

        result = del_controller_from_cloud(options)

        assert result == 0
        mock_config.assert_called_once_with(
            provider_name="", skip_conf_file=True, cloud_base_url="https://example.com"
        )
        mock_delete.assert_called_once_with(mock_settings)


@pytest.fixture
def daemon_mqtt(mock_mqtt_cloud_agent):
    mock_mqtt_cloud_agent.wait_for_connection.return_value = True
    mock_mqtt_cloud_agent.authentication_failed = False
    return mock_mqtt_cloud_agent


@pytest.fixture
def daemon_settings():
    settings = MagicMock()
    settings.cloud_base_url = "https://example.com"
    settings.broker_url = "tcp://localhost:1883"
    settings.request_period_seconds = 0
    settings.ping_period_seconds = 7
    with patch("wb.cloud_agent.commands.configure_app", return_value=settings):
        yield settings


@pytest.fixture
def send_stop():
    """
    Capture the daemon's signal handlers; the returned callable delivers SIGTERM to the daemon.
    """
    handlers = {}
    with patch("wb.cloud_agent.commands.signal.signal", side_effect=handlers.__setitem__):
        yield lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)


@pytest.fixture
def cloud_requests(send_stop):
    """
    Cloud handshake succeeds; the first event request asks the daemon to stop.
    """
    with (
        patch("wb.cloud_agent.commands.wait_for_cloud_reachable", return_value=True),
        patch("wb.cloud_agent.commands.make_start_up_request") as startup,
        patch("wb.cloud_agent.commands.send_packages_version"),
        patch("wb.cloud_agent.commands.read_activation_link", return_value="http://link"),
        patch("wb.cloud_agent.commands.reconcile_metrics_script"),
        patch("wb.cloud_agent.commands.make_event_request", side_effect=lambda *_: send_stop()) as events,
    ):
        yield SimpleNamespace(startup=startup, events=events)


DAEMON_OPTIONS = Namespace(provider_name="test", broker=None, config=None)


@pytest.mark.usefixtures("daemon_settings")
def test_run_daemon_stops_on_signal_with_success(daemon_mqtt, cloud_requests):
    """
    SIGTERM ends the event loop: the topics are removed, MQTT is stopped and the exit code is 0.
    """
    assert run_daemon(DAEMON_OPTIONS) == 0

    daemon_mqtt.start.assert_called_once_with(daemon=True)
    cloud_requests.events.assert_called_once()
    daemon_mqtt.remove_vdev.assert_called_once()
    daemon_mqtt.stop.assert_called_once()
    statuses = [c.args[1] for c in daemon_mqtt.publish_ctrl.call_args_list if c.args[0] == "status"]
    assert statuses == ["starting", "connecting", "ok"]


@pytest.mark.usefixtures("daemon_settings", "send_stop")
def test_run_daemon_exits_2_on_rejected_mqtt_login(daemon_mqtt):
    daemon_mqtt.wait_for_connection.return_value = False
    daemon_mqtt.authentication_failed = True

    assert run_daemon(DAEMON_OPTIONS) == 2

    daemon_mqtt.remove_vdev.assert_not_called()
    daemon_mqtt.stop.assert_called_once()


@pytest.mark.usefixtures("daemon_settings", "send_stop")
def test_run_daemon_stop_before_connection_is_success(daemon_mqtt):
    daemon_mqtt.wait_for_connection.return_value = False

    assert run_daemon(DAEMON_OPTIONS) == 0

    daemon_mqtt.remove_vdev.assert_called_once()
    daemon_mqtt.stop.assert_called_once()


@pytest.mark.usefixtures("send_stop")
def test_run_daemon_invalid_broker_in_config_is_not_configured(daemon_mqtt, daemon_settings):
    daemon_settings.broker_url = "mqtt://no-port"

    assert run_daemon(DAEMON_OPTIONS) == 6

    daemon_mqtt.start.assert_not_called()


@pytest.mark.usefixtures("daemon_mqtt", "cloud_requests")
def test_run_daemon_custom_broker_overrides_config(daemon_settings):
    options = Namespace(provider_name="test", broker="tcp://192.168.1.1:1883", config=None)

    assert run_daemon(options) == 0

    assert daemon_settings.broker_url == "tcp://192.168.1.1:1883"


@pytest.mark.usefixtures("daemon_settings")
def test_run_daemon_retries_startup_request(daemon_mqtt, cloud_requests):
    """
    A failed handshake is repeated instead of ending the daemon; the status control shows it.
    """
    cloud_requests.startup.side_effect = [CloudNetworkError("Startup failed"), None]

    assert run_daemon(DAEMON_OPTIONS) == 0

    assert cloud_requests.startup.call_count == 2
    daemon_mqtt.publish_ctrl.assert_any_call("status", "Network or Cloud is unreachable! Retrying...")


@pytest.mark.usefixtures("daemon_settings")
def test_run_daemon_event_loop_reports_errors_then_ok(daemon_mqtt, cloud_requests, send_stop):
    """
    Event request errors are published as the status and the loop goes on until it is stopped.
    """
    outcomes = iter([subprocess.TimeoutExpired("curl", 360), CloudNetworkError("Network error"), None])

    def next_event(*_):
        outcome = next(outcomes)
        if outcome is None:
            send_stop()
            return
        raise outcome

    cloud_requests.events.side_effect = next_event

    assert run_daemon(DAEMON_OPTIONS) == 0

    assert cloud_requests.events.call_count == 3
    statuses = [c.args[1] for c in daemon_mqtt.publish_ctrl.call_args_list if c.args[0] == "status"]
    assert statuses[-3:] == [
        "Request timeout. Retrying...",
        "Network or Cloud is unreachable! Retrying...",
        "ok",
    ]
