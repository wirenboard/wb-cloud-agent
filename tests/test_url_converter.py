# pylint: disable=unused-argument

from wb.cloud_agent.main import get_controller_url
from wb.cloud_agent.settings import AppSettings, base_url_to_agent_url


def test_base_url_to_agent_url():
    assert (
        base_url_to_agent_url("https://cloud-staging.wirenboard.com/")
        == "https://agent.cloud-staging.wirenboard.com/api-agent/v1/"
    )
    assert (
        base_url_to_agent_url("https://wirenboard.cloud/") == "https://agent.wirenboard.cloud/api-agent/v1/"
    )


def test_get_controller_url(mock_serial_number, settings: AppSettings):
    assert (
        get_controller_url(settings.cloud_base_url)
        == f"{settings.cloud_base_url}/controllers/{mock_serial_number}"
    )
