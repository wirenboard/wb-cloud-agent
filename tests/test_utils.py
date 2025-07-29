from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import get_controller_url, parse_headers


def test_base_url_to_agent_url(settings: AppSettings):
    assert (
        settings.base_url_to_agent_url("https://cloud-staging.wirenboard.com/")
        == "https://agent.cloud-staging.wirenboard.com/api-agent/v1/"
    )
    assert (
        settings.base_url_to_agent_url("https://wirenboard.cloud/")
        == "https://agent.wirenboard.cloud/api-agent/v1/"
    )


def test_get_controller_url(mock_serial_number, settings: AppSettings):
    assert (
        get_controller_url(settings.cloud_base_url)
        == f"{settings.cloud_base_url}/controllers/{mock_serial_number}"
    )


def test_parse_headers_basic():
    raw_headers = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "X-Poll-Interval: 30\r\n"
        "Cache-Control: no-cache\r\n"
    )
    headers = parse_headers(raw_headers)
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Poll-Interval"] == "30"
    assert headers["Cache-Control"] == "no-cache"
