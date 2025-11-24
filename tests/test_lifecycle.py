import time
from unittest.mock import patch

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.services.lifecycle import stop_services_and_del_configs


def test_stop_services_and_del_configs_unknown_link(settings, tmp_path):
    settings.activation_link_config = tmp_path / "activation_link.txt"
    settings.activation_link_config.write_text(UNKNOWN_LINK)

    provider_name = "test_provider"

    with (
        patch(
            "wb.cloud_agent.services.lifecycle.read_activation_link",
            return_value=UNKNOWN_LINK,
        ) as mock_read,
        patch("wb.cloud_agent.services.lifecycle.event_delete_controller", return_value=0) as mock_delete,
        patch("wb.cloud_agent.services.lifecycle.stop_and_disable_service") as mock_stop,
        patch("wb.cloud_agent.services.lifecycle.delete_provider_config") as mock_del_config,
        patch("builtins.print") as mock_print,
    ):
        stop_services_and_del_configs(settings, provider_name)

        mock_read.assert_called_once_with(settings)
        mock_delete.assert_called_once_with(settings)

        assert mock_stop.call_count == 3
        expected_services = [
            f"wb-cloud-agent@{provider_name}.service",
            f"wb-cloud-agent-frpc@{provider_name}.service",
            f"wb-cloud-agent-telegraf@{provider_name}.service",
        ]
        for service in expected_services:
            mock_stop.assert_any_call(service)

        assert mock_del_config.call_count == 2

        mock_print.assert_called_once_with(f"Provider {provider_name} successfully deleted")


def test_stop_services_and_del_configs_with_activation_link(settings, tmp_path):
    settings.activation_link_config = tmp_path / "activation_link.txt"
    settings.activation_link_config.write_text("http://example.com/activate")

    provider_name = "test_provider"

    with (
        patch(
            "wb.cloud_agent.services.lifecycle.read_activation_link",
            return_value="http://example.com/activate",
        ) as mock_read,
        patch("wb.cloud_agent.services.lifecycle.event_delete_controller") as mock_delete,
        patch("wb.cloud_agent.services.lifecycle.stop_and_disable_service") as mock_stop,
        patch("wb.cloud_agent.services.lifecycle.delete_provider_config") as mock_del_config,
        patch("builtins.print") as mock_print,
    ):
        stop_services_and_del_configs(settings, provider_name)

        mock_read.assert_called_once_with(settings)
        mock_delete.assert_not_called()
        assert mock_stop.call_count == 3
        assert mock_del_config.call_count == 2

        mock_print.assert_called_once()


def test_stop_services_and_del_configs_thread_timeout(settings, tmp_path):
    settings.activation_link_config = tmp_path / "activation_link.txt"
    settings.activation_link_config.write_text(UNKNOWN_LINK)

    provider_name = "test_provider"

    def slow_delete_controller(_):
        time.sleep(0.1)
        return 0

    with (
        patch(
            "wb.cloud_agent.services.lifecycle.read_activation_link",
            return_value=UNKNOWN_LINK,
        ),
        patch(
            "wb.cloud_agent.services.lifecycle.event_delete_controller",
            side_effect=slow_delete_controller,
        ),
        patch("wb.cloud_agent.services.lifecycle.stop_and_disable_service"),
        patch("wb.cloud_agent.services.lifecycle.delete_provider_config"),
        patch("builtins.print"),
    ):
        stop_services_and_del_configs(settings, provider_name)
