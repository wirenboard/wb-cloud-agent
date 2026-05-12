import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.services.activation import (
    read_activation_link,
    update_activation_link,
    write_activation_link,
)
from wb.cloud_agent.services.diagnostics import fetch_diagnostics
from wb.cloud_agent.services.metrics import update_metrics_config
from wb.cloud_agent.services.tunnel import update_tunnel_config


def test_read_activation_link_exists(settings, tmp_path):
    settings.activation_link_config = tmp_path / "activation_link.txt"
    settings.activation_link_config.write_text("http://example.com/activate\n")

    link = read_activation_link(settings)

    assert link == "http://example.com/activate"


def test_read_activation_link_not_exists(settings, tmp_path):
    settings.activation_link_config = tmp_path / "nonexistent.txt"

    link = read_activation_link(settings)

    assert link == UNKNOWN_LINK


def test_update_activation_link(settings):
    mock_mqtt = MagicMock()
    payload = {"activationLink": "http://example.com/new-activate"}

    with patch("wb.cloud_agent.services.activation.write_activation_link") as mock_write:
        update_activation_link(settings, payload, mock_mqtt)

        mock_write.assert_called_once_with(settings, "http://example.com/new-activate", mock_mqtt)


def test_write_activation_link(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.activation_link_config = tmp_path / "activation_link.txt"

    write_activation_link(settings, "http://example.com/activate", mock_mqtt)

    assert settings.activation_link_config.read_text() == "http://example.com/activate"
    mock_mqtt.publish_ctrl.assert_called_once_with("activation_link", "http://example.com/activate")


def test_update_tunnel_config(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.frp_config = tmp_path / "frpc.ini"
    settings.frp_service = "wb-cloud-agent-frpc@default.service"

    payload = {"config": "[common]\nserver_addr = 1.2.3.4"}

    with (
        patch("wb.cloud_agent.services.tunnel.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.tunnel.write_activation_link") as mock_write,
    ):
        update_tunnel_config(settings, payload, mock_mqtt)

        assert settings.frp_config.read_text() == "[common]\nserver_addr = 1.2.3.4"
        mock_service.assert_called_once_with(settings.frp_service, restart=True)
        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_update_metrics_config(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.metrics_script = tmp_path / "metrics_collector.py"
    settings.metrics_service = "wb-cloud-agent-metrics@default.service"
    settings.broker_url = "tcp://localhost:1883"

    payload = {"script": 'BROKER = "$BROKER_URL"'}

    with (
        patch("wb.cloud_agent.services.metrics.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active") as mock_active,
        patch("wb.cloud_agent.services.metrics.os.chmod"),
        patch("wb.cloud_agent.services.metrics.write_activation_link") as mock_write,
    ):
        update_metrics_config(settings, payload, mock_mqtt)

        content = settings.metrics_script.read_text()
        assert "tcp://localhost:1883" in content
        mock_service.assert_called_once_with(settings.metrics_service, restart=True)
        mock_active.assert_called_once_with(settings.metrics_service)
        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_update_metrics_config_template_substitution(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.metrics_script = tmp_path / "metrics_collector.py"
    settings.metrics_service = "wb-cloud-agent-metrics@default.service"
    settings.broker_url = "tcp://192.168.1.100:1883"

    payload = {"script": 'BROKER = "$BROKER_URL"\nPROVIDER = "$PROVIDER_NAME"'}

    with (
        patch("wb.cloud_agent.services.metrics.start_and_enable_service"),
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active"),
        patch("wb.cloud_agent.services.metrics.os.chmod"),
        patch("wb.cloud_agent.services.metrics.write_activation_link"),
    ):
        update_metrics_config(settings, payload, mock_mqtt)

        content = settings.metrics_script.read_text()
        assert "tcp://192.168.1.100:1883" in content
        assert "$BROKER_URL" not in content
        assert settings.provider_name in content


def test_update_metrics_config_script(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.metrics_script = tmp_path / "metrics_collector.py"
    settings.metrics_service = "wb-cloud-agent-metrics@default.service"
    settings.broker_url = "tcp://localhost:1883"

    payload = {"script": 'BROKER = "$BROKER_URL"\nPROVIDER = "$PROVIDER_NAME"'}

    with (
        patch("wb.cloud_agent.services.metrics.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active") as mock_active,
        patch("wb.cloud_agent.services.metrics.os.chmod") as mock_chmod,
        patch("wb.cloud_agent.services.metrics.write_activation_link") as mock_write,
    ):
        update_metrics_config(settings, payload, mock_mqtt)

        content = settings.metrics_script.read_text()
        assert "tcp://localhost:1883" in content
        assert "default" in content
        assert "$BROKER_URL" not in content
        mock_chmod.assert_called_once_with(settings.metrics_script, 0o755)
        mock_service.assert_called_once_with(settings.metrics_service, restart=True)
        mock_active.assert_called_once_with(settings.metrics_service)
        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_update_metrics_config_disabled(settings):
    mock_mqtt = MagicMock()

    with (
        patch("wb.cloud_agent.services.metrics._safe_stop_and_disable_service") as mock_stop,
        patch("wb.cloud_agent.services.metrics.write_activation_link") as mock_write,
    ):
        update_metrics_config(settings, {"enabled": False}, mock_mqtt)

        mock_stop.assert_called_once_with(settings.metrics_service)
        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_update_metrics_config_restarts_existing_monitor(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.metrics_script = tmp_path / "metrics_collector.py"
    settings.metrics_service = "wb-cloud-agent-metrics@default.service"
    settings.broker_url = "tcp://localhost:1883"

    old_stop_event = threading.Event()
    old_thread = MagicMock()
    old_thread.is_alive.return_value = True

    payload = {"script": 'BROKER = "$BROKER_URL"'}

    with (
        patch.dict(
            "wb.cloud_agent.services.metrics._monitor_threads",
            {settings.provider_name: old_thread},
            clear=True,
        ),
        patch.dict(
            "wb.cloud_agent.services.metrics._monitor_stop_events",
            {settings.provider_name: old_stop_event},
            clear=True,
        ),
        patch("wb.cloud_agent.services.metrics.start_and_enable_service"),
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active"),
        patch("wb.cloud_agent.services.metrics.os.chmod"),
        patch("wb.cloud_agent.services.metrics.write_activation_link"),
        patch("wb.cloud_agent.services.metrics.threading.Thread") as mock_thread,
    ):
        new_thread = MagicMock()
        mock_thread.return_value = new_thread

        update_metrics_config(settings, payload, mock_mqtt)

        assert old_stop_event.is_set()
        mock_thread.assert_called_once()
        new_thread.start.assert_called_once()


def test_update_metrics_config_without_script_fails_before_confirm(settings):
    with pytest.raises(ValueError, match="no collector script"):
        update_metrics_config(settings, {"enabled": True}, MagicMock())


def test_fetch_diagnostics(settings, tmp_path):
    settings.diag_archive = tmp_path
    mock_mqtt = MagicMock()

    old_file1 = tmp_path / "diag_old1.zip"
    old_file1.write_text("old data")
    old_file2 = tmp_path / "diag_old2.zip"
    old_file2.write_text("old data")

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("wb.cloud_agent.services.diagnostics.upload_diagnostic"),
    ):
        mock_process = MagicMock()
        mock_process.wait.return_value = None
        mock_popen.return_value.__enter__.return_value = mock_process

        fetch_diagnostics(settings, {}, mock_mqtt)

        assert not old_file1.exists()
        assert not old_file2.exists()

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert args[0] == "wb-diag-collect diag"
        assert kwargs["cwd"] == settings.diag_archive
        assert kwargs["shell"] is True


def test_fetch_diagnostics_deletion_error(settings, tmp_path):
    settings.diag_archive = tmp_path
    mock_mqtt = MagicMock()

    old_file = tmp_path / "diag_old.zip"
    old_file.write_text("old data")

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("wb.cloud_agent.services.diagnostics.upload_diagnostic"),
        patch.object(Path, "unlink", side_effect=OSError("Permission denied")),
    ):
        mock_process = MagicMock()
        mock_process.wait.return_value = None
        mock_popen.return_value.__enter__.return_value = mock_process

        fetch_diagnostics(settings, {}, mock_mqtt)
