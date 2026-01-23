from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.handlers.provider import delete_provider

# pylint: disable=redefined-outer-name  # pytest fixtures pattern


@pytest.fixture
def mock_provider_patches():
    """Common patches for delete_provider tests"""
    with (
        patch("wb.cloud_agent.handlers.provider.stop_and_disable_service") as mock_stop,
        patch("wb.cloud_agent.handlers.provider.delete_provider_config") as mock_del_config,
        patch("wb.cloud_agent.handlers.provider.logging") as mock_logging,
    ):
        yield {
            "stop": mock_stop,
            "del_config": mock_del_config,
            "logging": mock_logging,
        }


def test_delete_provider_success(settings, mock_provider_patches):
    """Test successful provider deletion with all services stopped and configs removed"""
    mocks = mock_provider_patches

    # Call the function (event data and mqtt client are ignored)
    delete_provider(settings, {}, None)

    # Verify services were stopped (all 3: frpc, telegraf, main agent)
    assert mocks["stop"].call_count == 3
    mocks["stop"].assert_any_call(f"wb-cloud-agent-frpc@{settings.provider_name}.service")
    mocks["stop"].assert_any_call(f"wb-cloud-agent-telegraf@{settings.provider_name}.service")
    mocks["stop"].assert_any_call(f"wb-cloud-agent@{settings.provider_name}.service")

    # Verify configs were deleted
    assert mocks["del_config"].call_count == 2

    # Verify logging calls
    mocks["logging"].debug.assert_any_call("Deleting provider: %s", settings.provider_name)
    mocks["logging"].info.assert_any_call("Provider %s successfully deleted", settings.provider_name)


def test_delete_provider_with_custom_provider_name(settings, mock_provider_patches):
    """Test that provider name is correctly used in all operations"""
    settings.provider_name = "custom-provider-123"
    mocks = mock_provider_patches

    delete_provider(settings, {}, None)

    # Verify custom provider name is used in all service calls
    mocks["stop"].assert_any_call("wb-cloud-agent-frpc@custom-provider-123.service")
    mocks["stop"].assert_any_call("wb-cloud-agent-telegraf@custom-provider-123.service")
    mocks["stop"].assert_any_call("wb-cloud-agent@custom-provider-123.service")


def test_delete_provider_stops_services_in_correct_order(settings, mock_provider_patches):
    """Test that services are stopped in correct order: auxiliary first, then main"""
    mocks = mock_provider_patches
    call_order = []

    def track_calls(service):
        call_order.append(service)

    mocks["stop"].side_effect = track_calls

    delete_provider(settings, {}, None)

    # Verify order: frpc and telegraf stopped before main agent
    assert call_order[0] == f"wb-cloud-agent-frpc@{settings.provider_name}.service"
    assert call_order[1] == f"wb-cloud-agent-telegraf@{settings.provider_name}.service"
    assert call_order[2] == f"wb-cloud-agent@{settings.provider_name}.service"


def test_delete_provider_ignores_event_and_mqtt_params(settings, mock_provider_patches):
    """Test that function works regardless of event data and mqtt client passed"""
    event_data = {"some": "data", "nested": {"key": "value"}}

    # Should not raise any errors with any event data or mqtt client
    delete_provider(settings, event_data, MagicMock())
    delete_provider(settings, {}, None)
    delete_provider(settings, None, MagicMock())

    # Verify function was called 3 times successfully
    assert mock_provider_patches["stop"].call_count == 9  # 3 calls * 3 services each
