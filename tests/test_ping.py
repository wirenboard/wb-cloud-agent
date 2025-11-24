import subprocess
from unittest.mock import MagicMock, patch

from wb.cloud_agent.handlers.ping import wait_for_ping


def test_wait_for_ping_success_first_try(mock_subprocess_run):
    mock_subprocess_run.return_value.returncode = 0

    wait_for_ping("8.8.8.8", period=1)

    mock_subprocess_run.assert_called_once_with(
        ["ping", "-c", "1", "-W", "2", "8.8.8.8"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_wait_for_ping_success_after_retries(mock_subprocess_run):
    # First two calls fail, third succeeds
    mock_subprocess_run.side_effect = [
        MagicMock(returncode=1),
        MagicMock(returncode=1),
        MagicMock(returncode=0),
    ]

    with patch("time.sleep") as mock_sleep:
        wait_for_ping("example.com", period=2)

        assert mock_subprocess_run.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(2)


def test_wait_for_ping_custom_period(mock_subprocess_run):
    mock_subprocess_run.side_effect = [
        MagicMock(returncode=1),
        MagicMock(returncode=0),
    ]

    with patch("time.sleep") as mock_sleep:
        wait_for_ping("192.168.1.1", period=10)

        mock_sleep.assert_called_once_with(10)
