from wb.cloud_agent.settings import AppSettings


def test_settings():
    settings = AppSettings()
    settings.apply_conf_file("tests/data/wb-cloud-agent.conf")
    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.CLOUD_BASE_URL == "https://example1.com"
