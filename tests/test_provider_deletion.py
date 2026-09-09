from unittest.mock import MagicMock, call, patch

import pytest

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.handlers.provider import delete_provider, unbind_provider

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

    # Verify services were stopped (all 3: frpc, metrics, main agent)
    assert mocks["stop"].call_count == 3
    mocks["stop"].assert_any_call(f"wb-cloud-agent-frpc@{settings.provider_name}.service")
    mocks["stop"].assert_any_call(f"wb-cloud-agent-metrics@{settings.provider_name}.service")
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
    mocks["stop"].assert_any_call("wb-cloud-agent-metrics@custom-provider-123.service")
    mocks["stop"].assert_any_call("wb-cloud-agent@custom-provider-123.service")


def test_delete_provider_stops_services_in_correct_order(settings, mock_provider_patches):
    """Test that services are stopped in correct order: auxiliary first, then main"""
    mocks = mock_provider_patches
    call_order = []

    def track_calls(service):
        call_order.append(service)

    mocks["stop"].side_effect = track_calls

    delete_provider(settings, {}, None)

    # Verify order: frpc and metrics stopped before main agent
    assert call_order[0] == f"wb-cloud-agent-frpc@{settings.provider_name}.service"
    assert call_order[1] == f"wb-cloud-agent-metrics@{settings.provider_name}.service"
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


def test_unbind_provider_preserves_identity_and_clears_runtime(settings, tmp_path):
    settings.config_file = tmp_path / "etc" / "providers" / settings.provider_name / "wb-cloud-agent.conf"
    settings.config_file.parent.mkdir(parents=True)
    settings.config_file.write_text("identity")
    runtime_dir = tmp_path / "var" / "providers" / settings.provider_name
    settings.frp_config = runtime_dir / "frpc.conf"
    settings.metrics_script = runtime_dir / "metrics_collector.py"
    settings.metrics_vars_config = runtime_dir / "metrics_collector.conf"
    settings.metrics_last_uid = runtime_dir / "metrics_last_uid"
    settings.activation_link_config = runtime_dir / "activation_link.conf"
    connection_token = runtime_dir / "connection.token"
    unrelated_runtime_state = runtime_dir / "future-state"
    runtime_dir.mkdir(parents=True)
    for attribute in (
        "frp_config",
        "metrics_script",
        "metrics_vars_config",
        "metrics_last_uid",
        "activation_link_config",
    ):
        getattr(settings, attribute).write_text("stale runtime state")
    connection_token.write_text("stale runtime state")
    unrelated_runtime_state.write_text("preserve this state")

    with (
        patch("wb.cloud_agent.handlers.provider.stop_and_disable_service") as mock_stop,
        patch("wb.cloud_agent.handlers.provider.stop_metrics_health_monitor") as mock_monitor,
    ):
        mqtt = MagicMock()
        unbind_provider(settings, {}, mqtt)
        unbind_provider(settings, {}, mqtt)

    mock_stop.assert_has_calls([call(settings.frp_service), call(settings.metrics_service)])
    mqtt.publish_ctrl.assert_any_call("activation_link", UNKNOWN_LINK)
    assert settings.config_file.read_text() == "identity"
    assert settings.activation_link_config.read_text() == UNKNOWN_LINK
    assert not settings.frp_config.exists()
    assert not settings.metrics_script.exists()
    assert not settings.metrics_vars_config.exists()
    assert not settings.metrics_last_uid.exists()
    assert not connection_token.exists()
    assert unrelated_runtime_state.read_text() == "preserve this state"

    assert mock_monitor.call_count == 2
    assert mock_stop.call_count == 4
    assert mqtt.publish_ctrl.call_count == 2


def test_unbind_provider_keeps_runtime_when_service_stop_fails(settings, tmp_path):
    settings.frp_config = tmp_path / "frpc.conf"
    settings.frp_config.write_text("stale")

    with (
        patch(
            "wb.cloud_agent.handlers.provider.stop_and_disable_service",
            side_effect=[None, RuntimeError("stop failed")],
        ),
        patch("wb.cloud_agent.handlers.provider.stop_metrics_health_monitor"),
    ):
        with pytest.raises(RuntimeError, match="stop failed"):
            unbind_provider(settings, {}, MagicMock())

    assert settings.frp_config.exists()
