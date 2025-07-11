from pathlib import Path

from wb.cloud_agent.settings import AppSettings


def test_settings():
    settings = AppSettings(provider_name="some_provider")
    settings.config_file = Path("tests/data/wb-cloud-agent.conf")
    settings.apply_conf_file()
    settings.cloud_agent_url = settings.base_url_to_agent_url(settings.cloud_base_url)
    assert settings.log_level == "DEBUG"
    assert settings.client_cert_engine_key == "NEW_ATECCx08:00:02:C0:00"
    assert settings.cloud_base_url == "https://example1.com"
    assert settings.cloud_agent_url == "https://agent.example1.com/api-agent/v1/"
