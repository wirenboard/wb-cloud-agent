from subprocess import CalledProcessError
from unittest.mock import MagicMock, call, patch

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.handlers.provider import unbind_provider
from wb.cloud_agent.services.activation import (
    write_activation_link as write_activation_link_impl,
)

# pylint: disable=redefined-outer-name  # pytest fixtures pattern


def test_unbind_provider_preserves_identity_and_clears_runtime(isolated_provider_runtime, tmp_path):
    settings = isolated_provider_runtime
    settings.config_file = tmp_path / "etc" / "providers" / settings.provider_name / "wb-cloud-agent.conf"
    settings.config_file.parent.mkdir(parents=True)
    settings.config_file.write_text("identity")
    runtime_dir = settings.activation_link_config.parent
    unrelated_runtime_state = runtime_dir / "future-state"
    for attribute in (
        "frp_config",
        "metrics_script",
        "metrics_vars_config",
        "metrics_last_uid",
        "activation_link_config",
    ):
        getattr(settings, attribute).write_text("stale runtime state")
    unrelated_runtime_state.write_text("preserve this state")
    link_contents_before_write = []

    def write_activation_link_and_capture(settings, link, mqtt):
        link_contents_before_write.append(settings.activation_link_config.read_text())
        write_activation_link_impl(settings, link, mqtt)

    with (
        patch("wb.cloud_agent.handlers.provider._safe_stop_and_disable_service") as mock_stop,
        patch("wb.cloud_agent.handlers.provider.stop_metrics_health_monitor") as mock_monitor,
        patch(
            "wb.cloud_agent.handlers.provider.write_activation_link",
            side_effect=write_activation_link_and_capture,
        ),
    ):
        mqtt = MagicMock()
        unbind_provider(settings, {}, mqtt)
        unbind_provider(settings, {}, mqtt)

    mock_stop.assert_has_calls([call(settings.frp_service), call(settings.metrics_service)])
    mqtt.publish_ctrl.assert_any_call("activation_link", UNKNOWN_LINK)
    assert settings.config_file.read_text() == "identity"
    assert settings.activation_link_config.read_text() == UNKNOWN_LINK
    assert link_contents_before_write == ["stale runtime state", UNKNOWN_LINK]
    assert not settings.frp_config.exists()
    assert not settings.metrics_script.exists()
    assert not settings.metrics_vars_config.exists()
    assert not settings.metrics_last_uid.exists()
    assert unrelated_runtime_state.read_text() == "preserve this state"

    assert mock_monitor.call_count == 2
    assert mock_stop.call_count == 4
    assert mqtt.publish_ctrl.call_count == 2


def test_unbind_provider_continues_after_service_stop_fails(isolated_provider_runtime):
    settings = isolated_provider_runtime
    settings.frp_config.write_text("stale")
    settings.activation_link_config.write_text("old-link")

    with (
        patch(
            "wb.cloud_agent.services.metrics.stop_and_disable_service",
            side_effect=CalledProcessError(1, ["systemctl", "stop"]),
        ),
        patch("wb.cloud_agent.handlers.provider.stop_metrics_health_monitor"),
    ):
        unbind_provider(settings, {}, MagicMock())

    assert not settings.frp_config.exists()
    assert settings.activation_link_config.read_text() == UNKNOWN_LINK
