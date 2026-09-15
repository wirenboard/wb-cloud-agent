import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from wb.cloud_agent.handlers.ping import wait_for_cloud_reachable


@pytest.mark.parametrize("status", [200, 204, 302])
def test_reachable_cloud_returns_success(status):
    with patch("requests.head", return_value=MagicMock(status_code=status)) as head:
        assert wait_for_cloud_reachable("https://unused.invalid", 5, threading.Event())
        head.assert_called_once_with("https://unused.invalid", timeout=15, allow_redirects=True)


@pytest.mark.parametrize("failure", [MagicMock(status_code=503), requests.ConnectionError("offline")])
def test_cloud_recovers_after_an_outage(failure):
    stop = MagicMock()
    stop.is_set.return_value = False
    with patch("requests.head", side_effect=[failure, MagicMock(status_code=200)]) as head:
        assert wait_for_cloud_reachable("https://unused.invalid", 7, stop)
        assert head.call_count == 2
        stop.wait.assert_called_once_with(7)


def test_stop_while_waiting_for_cloud():
    stop = threading.Event()
    with (
        patch("requests.head", return_value=MagicMock(status_code=503)) as head,
        patch.object(stop, "wait", side_effect=lambda _seconds: stop.set()),
    ):
        assert not wait_for_cloud_reachable("https://unused.invalid", 3600, stop)
        head.assert_called_once()
