from http import HTTPStatus as status
from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.handlers.events import (
    event_confirm,
    event_delete_controller,
    make_event_request,
)


def test_make_event_request_no_content(settings, mock_subprocess_run):
    mock_mqtt = MagicMock()
    headers = f"HTTP/1.1 {status.NO_CONTENT} No Content\r\n\r\n"
    body = ""
    meta = '{"code": "204"}'
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = stdout

    result = make_event_request(settings, mock_mqtt)  # pylint: disable=assignment-from-none

    assert result is None


def test_make_event_request_update_activation_link(settings):
    mock_mqtt = MagicMock()
    event_data = {
        "id": "event123",
        "code": "update_activation_link",
        "payload": {"activationLink": "http://example.com/activate"},
    }

    with (
        patch("wb.cloud_agent.services.activation.write_to_file") as mock_write,
        patch("wb.cloud_agent.handlers.events.do_curl") as mock_curl,
        patch("wb.cloud_agent.handlers.events.event_confirm") as mock_confirm,
    ):
        mock_curl.return_value = (event_data, status.OK)

        make_event_request(settings, mock_mqtt)

        mock_write.assert_called_once()
        mock_mqtt.publish_ctrl.assert_called_once_with("activation_link", "http://example.com/activate")
        mock_confirm.assert_called_once_with(settings, "event123")


def test_make_event_request_update_tunnel_config(settings):
    mock_mqtt = MagicMock()
    event_data = {
        "id": "event456",
        "code": "update_tunnel_config",
        "payload": {"config": "tunnel config content"},
    }

    with (
        patch("wb.cloud_agent.services.tunnel.write_to_file") as mock_write,
        patch("wb.cloud_agent.services.tunnel.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.tunnel.write_activation_link") as mock_link,
        patch("wb.cloud_agent.handlers.events.do_curl") as mock_curl,
        patch("wb.cloud_agent.handlers.events.event_confirm") as mock_confirm,
    ):
        mock_curl.return_value = (event_data, status.OK)

        make_event_request(settings, mock_mqtt)

        mock_write.assert_called_once()
        mock_service.assert_called_once()
        mock_link.assert_called_once()
        mock_confirm.assert_called_once_with(settings, "event456")


def test_make_event_request_update_metrics_config(settings):
    mock_mqtt = MagicMock()
    event_data = {
        "id": "event789",
        "code": "update_metrics_config",
        "payload": {"config": "metrics config content"},
    }

    with (
        patch("wb.cloud_agent.services.metrics.write_to_file") as mock_write,
        patch("wb.cloud_agent.services.metrics.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.metrics.write_activation_link") as mock_link,
        patch("wb.cloud_agent.handlers.events.do_curl") as mock_curl,
        patch("wb.cloud_agent.handlers.events.event_confirm") as mock_confirm,
    ):
        mock_curl.return_value = (event_data, status.OK)

        make_event_request(settings, mock_mqtt)

        mock_write.assert_called_once()
        mock_service.assert_called_once()
        mock_link.assert_called_once()
        mock_confirm.assert_called_once_with(settings, "event789")


def test_make_event_request_fetch_diagnostics(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.diag_archive = tmp_path
    event_data = {
        "id": "event999",
        "code": "fetch_diagnostics",
        "payload": {"some": "data"},
    }

    with (
        patch("wb.cloud_agent.services.diagnostics.subprocess.Popen") as mock_popen,
        patch("wb.cloud_agent.services.diagnostics.upload_diagnostic"),
        patch("wb.cloud_agent.handlers.events.do_curl") as mock_curl,
        patch("wb.cloud_agent.handlers.events.event_confirm") as mock_confirm,
    ):
        mock_process = MagicMock()
        mock_process.wait.return_value = None
        mock_popen.return_value.__enter__.return_value = mock_process

        mock_curl.return_value = (event_data, status.OK)

        make_event_request(settings, mock_mqtt)

        mock_popen.assert_called_once()
        mock_confirm.assert_called_once_with(settings, "event999")


def test_make_event_request_unknown_event(settings):
    mock_mqtt = MagicMock()
    event_data = {
        "id": "event000",
        "code": "unknown_event_code",
        "payload": {"data": "something"},
    }

    with (
        patch("wb.cloud_agent.handlers.events.do_curl") as mock_curl,
        patch("wb.cloud_agent.handlers.events.event_confirm") as mock_confirm,
    ):
        mock_curl.return_value = (event_data, status.OK)

        # Should not raise, just log warning
        make_event_request(settings, mock_mqtt)
        mock_confirm.assert_called_once_with(settings, "event000")


def test_make_event_request_missing_event_id(settings):
    mock_mqtt = MagicMock()
    event_data = {
        "code": "update_activation_link",
        "payload": {"activationLink": "http://example.com/activate"},
    }

    with patch("wb.cloud_agent.handlers.events.do_curl") as mock_curl:
        mock_curl.return_value = (event_data, status.OK)

        with pytest.raises(ValueError, match="Unknown event id"):
            make_event_request(settings, mock_mqtt)


def test_make_event_request_empty_payload(settings):
    mock_mqtt = MagicMock()
    event_data = {"id": "event123", "code": "update_activation_link", "payload": None}

    with patch("wb.cloud_agent.handlers.events.do_curl") as mock_curl:
        mock_curl.return_value = (event_data, status.OK)

        with pytest.raises(ValueError, match="Empty payload"):
            make_event_request(settings, mock_mqtt)


def test_event_confirm_invalid_status(settings, mock_subprocess_run):
    headers = f"HTTP/1.1 {status.BAD_REQUEST} Bad Request\r\n\r\n"
    body = '{"error": "bad request"}'
    meta = '{"code": "400"}'
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = stdout

    with pytest.raises(ValueError, match="Not a 204 status on event confirmation"):
        event_confirm(settings, "event123")


def test_event_delete_controller_success(settings, mock_subprocess_run):
    headers = f"HTTP/1.1 {status.NO_CONTENT} No Content\r\n\r\n"
    body = ""
    meta = '{"code": "204"}'
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = stdout

    result = event_delete_controller(settings)

    assert result == 0


def test_event_delete_controller_network_error(settings, mock_subprocess_run):
    mock_subprocess_run.side_effect = Exception("Network error")

    result = event_delete_controller(settings)

    assert result == 1


def test_event_delete_controller_invalid_status(settings, mock_subprocess_run):
    headers = f"HTTP/1.1 {status.BAD_REQUEST} Bad Request\r\n\r\n"
    body = '{"error": "bad request"}'
    meta = '{"code": "400"}'
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = stdout

    result = event_delete_controller(settings)

    assert result == 1
