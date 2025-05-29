import subprocess
from unittest.mock import Mock, patch

import pytest

from wb.cloud_agent.main import run_daemon


def test_run_daemon_handles_calledprocesserror_once(settings):
    mqtt = Mock()

    err_msg = "Error making request to cloud!"

    with (
        patch("wb.cloud_agent.main.logging.exception") as mock_log_exception,
        patch("wb.cloud_agent.main.make_event_request") as mock_request,
    ):
        mock_request.side_effect = subprocess.CalledProcessError(
            returncode=57,
            cmd=["curl"],
            output=b"",
            stderr=b"some low-level curl error",
        )

        def stop_after_error_status(*args, **_):
            if args[0] == "status" and err_msg in args[1]:
                raise KeyboardInterrupt

        mqtt.publish_ctrl.side_effect = stop_after_error_status

        with pytest.raises(KeyboardInterrupt):
            run_daemon(mqtt, settings)

    mqtt.publish_ctrl.assert_called_with("status", err_msg)

    mock_log_exception.assert_called_once()
    args, _ = mock_log_exception.call_args
    assert args[0] == err_msg
