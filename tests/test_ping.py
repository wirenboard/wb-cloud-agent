from unittest.mock import MagicMock, patch

import requests

from wb.cloud_agent.handlers.ping import wait_for_cloud_reachable


def test_wait_for_cloud_success_first_try():
    """HEAD возвращает 200 с первой попытки — сразу выходим."""
    with patch("requests.head") as mock_head:
        mock_head.return_value.status_code = 200

        wait_for_cloud_reachable("https://example.com", period=5)

        mock_head.assert_called_once_with("https://example.com", timeout=15, allow_redirects=True)


def test_wait_for_cloud_success_after_retries():
    """HEAD два раза возвращает ошибку, на третьей попытке — OK."""
    with patch("requests.head") as mock_head, patch("time.sleep") as mock_sleep:
        mock_head.side_effect = [
            MagicMock(status_code=500),  # fail
            MagicMock(status_code=404),  # fail
            MagicMock(status_code=200),  # success
        ]

        wait_for_cloud_reachable("https://cloud", period=2)

        assert mock_head.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(2)


def test_wait_for_cloud_exception_then_success():
    """HEAD первый раз бросает исключение, затем успешный ответ."""
    with patch("requests.head") as mock_head, patch("time.sleep") as mock_sleep:
        mock_head.side_effect = [
            requests.RequestException("DNS error"),  # exception
            MagicMock(status_code=200),  # success
        ]

        wait_for_cloud_reachable("https://cloud", period=3)

        assert mock_head.call_count == 2
        mock_sleep.assert_called_once_with(3)


def test_wait_for_cloud_custom_period():
    """Проверяем кастомный period."""
    with patch("requests.head") as mock_head, patch("time.sleep") as mock_sleep:
        mock_head.side_effect = [
            MagicMock(status_code=503),  # fail
            MagicMock(status_code=200),  # OK
        ]

        wait_for_cloud_reachable("http://localhost", period=10)

        mock_sleep.assert_called_once_with(10)
