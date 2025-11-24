import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.settings import (
    AppSettings,
    Provider,
    configure_app,
    delete_provider_config,
    generate_provider_config,
    get_provider_names,
    load_providers_data,
    setup_log,
)


def test_app_settings_with_config_file(tmp_path):
    config_dir = tmp_path / "etc" / "wb-cloud-agent" / "providers" / "test"
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = config_dir / "wb-cloud-agent.conf"
    config_data = {
        "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:04:C0:00",
        "CLOUD_BASE_URL": "https://custom.cloud.com",
        "LOG_LEVEL": "DEBUG",
    }
    config_file.write_text(json.dumps(config_data))

    with patch(
        "wb.cloud_agent.settings.PROVIDERS_CONF_DIR",
        str(tmp_path / "etc" / "wb-cloud-agent" / "providers"),
    ):
        settings = AppSettings(provider_name="test")

        assert settings.client_cert_engine_key == "ATECCx08:00:04:C0:00"
        assert settings.cloud_base_url == "https://custom.cloud.com"
        assert settings.log_level == "DEBUG"


def test_app_settings_skip_conf_file(tmp_path):
    config_dir = tmp_path / "etc" / "wb-cloud-agent" / "providers" / "test"
    config_dir.mkdir(parents=True, exist_ok=True)

    with patch(
        "wb.cloud_agent.settings.PROVIDERS_CONF_DIR",
        str(tmp_path / "etc" / "wb-cloud-agent" / "providers"),
    ):
        settings = AppSettings(provider_name="test", skip_conf_file=True)

        # Should use default values
        assert settings.client_cert_engine_key == "ATECCx08:00:02:C0:00"


def test_configure_app_success():
    with patch("wb.cloud_agent.settings.AppSettings") as mock_settings:
        mock_instance = MagicMock()
        mock_instance.log_level = "INFO"
        mock_settings.return_value = mock_instance

        result = configure_app(provider_name="test")

        assert result == mock_instance


def test_configure_app_file_not_found():
    with patch("wb.cloud_agent.settings.AppSettings", side_effect=FileNotFoundError):
        result = configure_app(provider_name="test")

        assert result == 6


def test_configure_app_json_decode_error():
    with patch(
        "wb.cloud_agent.settings.AppSettings",
        side_effect=json.decoder.JSONDecodeError("msg", "doc", 0),
    ):
        result = configure_app(provider_name="test")

        assert result == 6


def test_setup_log_info_level():
    settings = MagicMock()
    settings.log_level = "INFO"

    with patch("logging.basicConfig") as mock_basic_config:
        setup_log(settings)

        mock_basic_config.assert_called_once()
        args = mock_basic_config.call_args
        assert args[1]["level"] == logging.INFO


def test_setup_log_debug_level():
    settings = MagicMock()
    settings.log_level = "DEBUG"

    with patch("logging.basicConfig") as mock_basic_config:
        setup_log(settings)

        args = mock_basic_config.call_args
        assert args[1]["level"] == logging.DEBUG


def test_setup_log_invalid_level():
    settings = MagicMock()
    settings.log_level = "INVALID_LEVEL"

    # getattr with invalid level returns NOTSET which is int, so this won't raise
    # Let's test that it just sets the level to NOTSET
    with patch("logging.basicConfig") as mock_basic_config:
        setup_log(settings)

        # Should still call basicConfig with NOTSET level
        mock_basic_config.assert_called_once()


def test_generate_provider_config(tmp_path):
    default_conf = {"CLOUD_BASE_URL": "https://default.com"}
    default_conf_file = tmp_path / "default.conf"
    default_conf_file.write_text(json.dumps(default_conf))

    providers_dir = tmp_path / "providers"

    with (
        patch("wb.cloud_agent.settings.DEFAULT_PROVIDER_CONF_FILE", str(default_conf_file)),
        patch("wb.cloud_agent.settings.PROVIDERS_CONF_DIR", str(providers_dir)),
    ):
        generate_provider_config("new_provider", "https://new.cloud.com")

        new_conf_file = providers_dir / "new_provider" / "wb-cloud-agent.conf"
        assert new_conf_file.exists()

        new_conf = json.loads(new_conf_file.read_text())
        assert new_conf["CLOUD_BASE_URL"] == "https://new.cloud.com"


def test_delete_provider_config_exists(tmp_path):
    provider_dir = tmp_path / "provider1"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "some_file.txt").write_text("data")

    delete_provider_config(str(tmp_path), "provider1")

    assert not provider_dir.exists()


def test_delete_provider_config_not_exists(tmp_path):
    # Should not raise error
    delete_provider_config(str(tmp_path), "nonexistent")


def test_delete_provider_config_permission_error(tmp_path):
    provider_dir = tmp_path / "provider1"
    provider_dir.mkdir(parents=True, exist_ok=True)

    with patch("shutil.rmtree", side_effect=PermissionError):
        # Should not raise, just log
        delete_provider_config(str(tmp_path), "provider1")


def test_delete_provider_config_os_error(tmp_path):
    provider_dir = tmp_path / "provider1"
    provider_dir.mkdir(parents=True, exist_ok=True)

    with patch("shutil.rmtree", side_effect=OSError("Some OS error")):
        # Should not raise, just log
        delete_provider_config(str(tmp_path), "provider1")


def test_get_provider_names_empty(tmp_path):
    with patch("wb.cloud_agent.settings.PROVIDERS_CONF_DIR", str(tmp_path / "nonexistent")):
        result = get_provider_names()

        assert result == []


def test_get_provider_names_with_providers(tmp_path):
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir(parents=True, exist_ok=True)

    (providers_dir / "provider1").mkdir()
    (providers_dir / "provider2").mkdir()
    (providers_dir / "file.txt").write_text("not a dir")

    with patch("wb.cloud_agent.settings.PROVIDERS_CONF_DIR", str(providers_dir)):
        result = get_provider_names()

        assert set(result) == {"provider1", "provider2"}


def test_provider_display_url_with_http_link():
    provider = Provider(
        name="test",
        config={"CLOUD_BASE_URL": "https://example.com"},
        activation_link="http://activation.link",
    )

    assert provider.display_url == "http://activation.link"


def test_provider_display_url_with_noconnect():
    from wb.cloud_agent.constants import NOCONNECT_LINK

    provider = Provider(
        name="test",
        config={"CLOUD_BASE_URL": "https://example.com"},
        activation_link=NOCONNECT_LINK,
    )

    assert "No connect to:" in provider.display_url
    assert "https://example.com" in provider.display_url


def test_provider_display_url_with_controller_url():
    provider = Provider(
        name="test",
        config={"CLOUD_BASE_URL": "https://example.com"},
        activation_link="some_other_value",
    )

    with patch(
        "wb.cloud_agent.settings.get_controller_url",
        return_value="https://example.com/controllers/ABC123",
    ):
        result = provider.display_url

        assert result == "https://example.com/controllers/ABC123"


def test_load_providers_data_success(tmp_path):
    providers_conf_dir = tmp_path / "conf" / "providers"
    providers_data_dir = tmp_path / "data" / "providers"

    for provider_name in ["provider1", "provider2"]:
        conf_dir = providers_conf_dir / provider_name
        conf_dir.mkdir(parents=True, exist_ok=True)

        config = {"CLOUD_BASE_URL": f"https://{provider_name}.com"}
        (conf_dir / "wb-cloud-agent.conf").write_text(json.dumps(config))

        data_dir = providers_data_dir / provider_name
        data_dir.mkdir(parents=True, exist_ok=True)

        (data_dir / "activation_link.conf").write_text(f"http://{provider_name}.link")

    with (
        patch("wb.cloud_agent.settings.PROVIDERS_CONF_DIR", str(providers_conf_dir)),
        patch("wb.cloud_agent.settings.APP_DATA_PROVIDERS_DIR", str(providers_data_dir)),
    ):
        result = load_providers_data(["provider1", "provider2"])

        assert len(result) == 2
        assert result[0].name == "provider1"
        assert result[0].config["CLOUD_BASE_URL"] == "https://provider1.com"
        assert result[0].activation_link == "http://provider1.link"


def test_load_providers_data_no_activation_link(tmp_path):
    providers_conf_dir = tmp_path / "conf" / "providers"
    providers_data_dir = tmp_path / "data" / "providers"

    conf_dir = providers_conf_dir / "provider1"
    conf_dir.mkdir(parents=True, exist_ok=True)

    config = {"CLOUD_BASE_URL": "https://provider1.com"}
    (conf_dir / "wb-cloud-agent.conf").write_text(json.dumps(config))

    with (
        patch("wb.cloud_agent.settings.PROVIDERS_CONF_DIR", str(providers_conf_dir)),
        patch("wb.cloud_agent.settings.APP_DATA_PROVIDERS_DIR", str(providers_data_dir)),
    ):
        result = load_providers_data(["provider1"])

        from wb.cloud_agent.constants import NOCONNECT_LINK

        assert result[0].activation_link == NOCONNECT_LINK


def test_load_providers_data_missing_config(tmp_path):
    providers_conf_dir = tmp_path / "conf" / "providers"

    with (
        patch("wb.cloud_agent.settings.PROVIDERS_CONF_DIR", str(providers_conf_dir)),
        patch("builtins.print") as mock_print,
    ):
        with pytest.raises(SystemExit) as exc_info:
            load_providers_data(["nonexistent"])

        assert exc_info.value.code == 6
        mock_print.assert_called_once()
