from wb.cloud_agent.settings import AppSettings


def test_settings(tmp_path):
    config_file = tmp_path / "wb-cloud-agent.conf"
    config_file.write_text(
        '{"LOG_LEVEL": "DEBUG", "CLIENT_CERT_ENGINE_KEY": "NEW_ATECCx08:00:02:C0:00", '
        '"CLOUD_BASE_URL": "https://example1.com"}'
    )
    settings = AppSettings(provider_name="some_provider")
    settings.config_file = config_file
    settings.apply_conf_file()
    settings.cloud_agent_url = settings.base_url_to_agent_url(settings.cloud_base_url)
    assert settings.log_level == "DEBUG"
    assert settings.client_cert_engine_key == "NEW_ATECCx08:00:02:C0:00"
    assert settings.cloud_base_url == "https://example1.com"
    assert settings.cloud_agent_url == "https://agent.example1.com/api-agent/v1/"
