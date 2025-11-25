# pylint: disable=redefined-outer-name

import subprocess
from argparse import Namespace
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
    options = Namespace(base_url="https://example.com", name=None)

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
    options = Namespace(base_url="https://example.com", name="custom_name")

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
        mock_gen.assert_called_once_with("custom_name", "https://example.com")


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


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_run_daemon_startup_failure():
    options = Namespace(provider_name="test", broker=None)

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.wait_for_ping"),
        patch(
            "wb.cloud_agent.commands.make_start_up_request",
            side_effect=RuntimeError("Startup failed"),
        ),
    ):
        mock_settings = MagicMock()
        mock_settings.cloud_base_url = "https://example.com"
        mock_settings.broker_url = "tcp://localhost:1883"
        mock_settings.request_period_seconds = 10
        mock_config.return_value = mock_settings

        result = run_daemon(options)

        assert result == 1


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_run_daemon_with_custom_broker():
    options = Namespace(provider_name="test", broker="tcp://192.168.1.1:1883")

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.wait_for_ping"),
        patch("wb.cloud_agent.commands.make_start_up_request"),
        patch("wb.cloud_agent.commands.send_agent_version"),
        patch("wb.cloud_agent.commands.read_activation_link", return_value="http://link"),
        patch("wb.cloud_agent.commands.make_event_request"),
        patch("time.sleep", side_effect=KeyboardInterrupt),
    ):  # Stop the loop
        mock_settings = MagicMock()
        mock_settings.cloud_base_url = "https://example.com"
        mock_settings.broker_url = "tcp://localhost:1883"
        mock_settings.request_period_seconds = 10
        mock_config.return_value = mock_settings

        try:
            run_daemon(options)
        except KeyboardInterrupt:
            # Expected interruption to stop the daemon loop during testing.
            pass

        assert mock_settings.broker_url == "tcp://192.168.1.1:1883"


@pytest.mark.usefixtures("mock_mqtt_cloud_agent")
def test_run_daemon_event_loop_with_timeout():
    options = Namespace(provider_name="test", broker=None)

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.wait_for_ping"),
        patch("wb.cloud_agent.commands.make_start_up_request"),
        patch("wb.cloud_agent.commands.send_agent_version"),
        patch("wb.cloud_agent.commands.read_activation_link", return_value="http://link"),
        patch("wb.cloud_agent.commands.make_event_request") as mock_event,
        patch("time.sleep"),
    ):
        mock_settings = MagicMock()
        mock_settings.cloud_base_url = "https://example.com"
        mock_settings.broker_url = "tcp://localhost:1883"
        mock_settings.request_period_seconds = 10
        mock_config.return_value = mock_settings

        mock_event.side_effect = [
            subprocess.TimeoutExpired("curl", 360),
            KeyboardInterrupt(),
        ]

        try:
            run_daemon(options)
        except KeyboardInterrupt:
            # Expected interruption to stop the daemon loop during testing.
            pass

        # Should have been called twice
        assert mock_event.call_count == 2


def test_run_daemon_event_loop_with_exception(mock_mqtt_cloud_agent):
    options = Namespace(provider_name="test", broker=None)

    with (
        patch("wb.cloud_agent.commands.configure_app") as mock_config,
        patch("wb.cloud_agent.commands.wait_for_ping"),
        patch("wb.cloud_agent.commands.make_start_up_request"),
        patch("wb.cloud_agent.commands.send_agent_version"),
        patch("wb.cloud_agent.commands.read_activation_link", return_value="http://link"),
        patch("wb.cloud_agent.commands.make_event_request") as mock_event,
        patch("time.sleep"),
    ):
        mock_settings = MagicMock()
        mock_settings.cloud_base_url = "https://example.com"
        mock_settings.broker_url = "tcp://localhost:1883"
        mock_settings.request_period_seconds = 10
        mock_config.return_value = mock_settings

        # First call: Exception, second call: success and status ok, third: KeyboardInterrupt
        mock_event.side_effect = [
            RuntimeError("Network error"),
            None,
            KeyboardInterrupt(),
        ]

        try:
            run_daemon(options)
        except KeyboardInterrupt:
            # Expected interruption to stop the daemon loop during testing.
            pass

        # Should publish error status first, then ok status
        status_calls = [
            call for call in mock_mqtt_cloud_agent.publish_ctrl.call_args_list if call[0][0] == "status"
        ]
        assert len(status_calls) >= 2
