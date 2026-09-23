from unittest.mock import MagicMock, call, patch

import requests

from wb.cloud_agent.handlers.ping import wait_for_cloud_reachable


def make_stop_event(*is_set):
    """
    A stop event whose is_set() answers are scripted; wait() returns at once.
    """
    stop = MagicMock()
    stop.is_set.side_effect = list(is_set)
    stop.wait.return_value = False
    return stop


def test_wait_for_cloud_success_first_try():
    stop = make_stop_event(False)
    with patch("requests.head") as mock_head:
        mock_head.return_value.status_code = 200

        assert wait_for_cloud_reachable("https://example.com", 5, stop) is True

    mock_head.assert_called_once_with("https://example.com", timeout=15, allow_redirects=True)
    stop.wait.assert_not_called()


def test_wait_for_cloud_success_after_retries():
    stop = make_stop_event(False, False, False)
    with patch("requests.head") as mock_head:
        mock_head.side_effect = [
            MagicMock(status_code=500),  # fail
            MagicMock(status_code=404),  # fail
            MagicMock(status_code=200),  # success
        ]

        assert wait_for_cloud_reachable("https://cloud", 2, stop) is True

    assert mock_head.call_count == 3
    assert stop.wait.call_args_list == [call(2), call(2)]


def test_wait_for_cloud_exception_then_success():
    stop = make_stop_event(False, False)
    with patch("requests.head") as mock_head:
        mock_head.side_effect = [
            requests.RequestException("DNS error"),  # exception
            MagicMock(status_code=200),  # success
        ]

        assert wait_for_cloud_reachable("https://cloud", 3, stop) is True

    assert mock_head.call_count == 2
    stop.wait.assert_called_once_with(3)


def test_wait_for_cloud_stops_on_request():
    """
    A stop requested while the cloud is down ends the wait with False instead of another attempt.
    """
    stop = make_stop_event(False, True)
    with patch("requests.head") as mock_head:
        mock_head.return_value.status_code = 503

        assert wait_for_cloud_reachable("http://localhost", 10, stop) is False

    mock_head.assert_called_once()
    stop.wait.assert_called_once_with(10)
