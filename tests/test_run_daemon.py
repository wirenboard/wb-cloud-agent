import subprocess
from unittest.mock import Mock, patch

import pytest

from wb.cloud_agent.main import run_daemon


def test_run_daemon_handles_calledprocesserror_once():
    mqtt = Mock()
    err_msg = "Error making request to cloud!"

    with (
        patch("wb.cloud_agent.main.make_start_up_request"),
        patch("wb.cloud_agent.main.send_agent_version"),
        patch("wb.cloud_agent.main.update_providers_list"),
        patch("wb.cloud_agent.main.configure_app") as mock_configure_app,
        patch("wb.cloud_agent.main.read_activation_link", return_value="mock_link"),
        patch("wb.cloud_agent.main.MQTTCloudAgent", return_value=mqtt),
        patch(
            "wb.cloud_agent.main.make_event_request",
            side_effect=subprocess.CalledProcessError(
                returncode=57, cmd=["curl"], output=b"", stderr=b"some low-level curl error"
            ),
        ),
        patch("wb.cloud_agent.main.time.sleep", side_effect=KeyboardInterrupt),
        patch("wb.cloud_agent.main.logging.exception") as mock_log_exception,
    ):
        mock_settings = Mock()
        mock_configure_app.return_value = mock_settings

        options = Mock(provider_name="test", broker=None)

        with pytest.raises(KeyboardInterrupt):
            run_daemon(options)

        mqtt.publish_ctrl.assert_any_call("status", err_msg)
        mock_log_exception.assert_called_once_with(err_msg)
