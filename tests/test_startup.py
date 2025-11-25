from http import HTTPStatus as status
from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.handlers.startup import (
    make_start_up_request,
    on_message,
    send_agent_version,
)
from wb.cloud_agent.constants import UNKNOWN_LINK


def test_make_start_up_request_activated(settings):
    mock_mqtt = MagicMock()
    status_data = {"activated": True, "activationLink": "http://example.com/activate"}

    with (
        patch("wb.cloud_agent.handlers.startup.write_activation_link") as mock_write,
        patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl,
    ):
        mock_curl.return_value = (status_data, status.OK)

        result = make_start_up_request(settings, mock_mqtt)

        assert result == status_data

        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_make_start_up_request_not_activated_with_link(settings):
    mock_mqtt = MagicMock()
    status_data = {
        "activated": False,
        "activationLink": "http://example.com/activate123",
    }

    with (
        patch("wb.cloud_agent.handlers.startup.write_activation_link") as mock_write,
        patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl,
    ):
        mock_curl.return_value = (status_data, status.OK)

        result = make_start_up_request(settings, mock_mqtt)

        assert result == status_data
        mock_write.assert_called_once_with(settings, "http://example.com/activate123", mock_mqtt)


def test_make_start_up_request_not_activated_no_link(settings):
    mock_mqtt = MagicMock()
    status_data = {"activated": False, "activationLink": ""}

    with (
        patch("wb.cloud_agent.handlers.startup.write_activation_link") as mock_write,
        patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl,
    ):
        mock_curl.return_value = (status_data, status.OK)

        result = make_start_up_request(settings, mock_mqtt)

        assert result == status_data

        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_make_start_up_request_invalid_status(settings):
    mock_mqtt = MagicMock()
    with patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl:
        mock_curl.return_value = ({}, status.BAD_REQUEST)

        with pytest.raises(ValueError, match="Not a 200 status while making start up request"):
            make_start_up_request(settings, mock_mqtt)


def test_make_start_up_request_missing_activated_field(settings):
    mock_mqtt = MagicMock()
    status_data = {"activationLink": "http://example.com/activate"}

    with patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl:
        mock_curl.return_value = (status_data, status.OK)

        with pytest.raises(ValueError, match="Invalid response data while making start up request"):
            make_start_up_request(settings, mock_mqtt)


def test_make_start_up_request_missing_activation_link_field(settings):
    mock_mqtt = MagicMock()
    status_data = {"activated": True}

    with patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl:
        mock_curl.return_value = (status_data, status.OK)

        with pytest.raises(ValueError, match="Invalid response data while making start up request"):
            make_start_up_request(settings, mock_mqtt)


def test_send_agent_version_success(settings):
    with patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl:
        mock_curl.return_value = ({"result": "ok"}, status.OK)

        send_agent_version(settings)

        mock_curl.assert_called_once()
        args = mock_curl.call_args
        assert args[1]["method"] == "put"
        assert args[1]["endpoint"] == "update_device_data/"
        assert "agent_version" in args[1]["params"]


def test_send_agent_version_failure(settings):
    with patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl:
        mock_curl.return_value = ({"error": "bad request"}, status.BAD_REQUEST)

        send_agent_version(settings)


def test_on_message_success(settings):
    with patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl:
        mock_curl.return_value = ({"result": "ok"}, status.OK)

        userdata = {"settings": settings}
        message = MagicMock()
        message.payload = b"WB7-v3.5"

        on_message(userdata, message)

        mock_curl.assert_called_once()
        args = mock_curl.call_args
        assert args[1]["method"] == "put"
        assert args[1]["endpoint"] == "update_device_data/"
        assert args[1]["params"]["hardware_revision"] == "WB7-v3.5"


def test_on_message_invalid_status(settings):
    with patch("wb.cloud_agent.handlers.startup.do_curl") as mock_curl:
        mock_curl.return_value = ({"error": "bad request"}, status.BAD_REQUEST)

        userdata = {"settings": settings}
        message = MagicMock()
        message.payload = b"WB7-v3.5"

        with pytest.raises(ValueError, match="Not a 200 status while making start up request"):
            on_message(userdata, message)
