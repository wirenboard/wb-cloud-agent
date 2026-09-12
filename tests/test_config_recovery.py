import json
import logging
from pathlib import Path
from unittest.mock import patch

from tests.conftest import PACKAGED_DEFAULT
from wb.cloud_agent.settings import AppSettings, load_providers_data


def write_config(dirs, provider, contents: str) -> Path:
    config = dirs.providers / provider / "wb-cloud-agent.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(contents, encoding="utf-8")
    return config


def broken_copies(config: Path) -> list:
    return sorted(config.parent.glob(f"{config.name}.broken-*"))


def test_healthy_config_is_never_rewritten(cloud_dirs):
    config = write_config(cloud_dirs, "wirenboard.cloud", '{"CLOUD_BASE_URL": "https://wirenboard.cloud"}')
    mtime = config.stat().st_mtime_ns

    settings = AppSettings(provider_name="wirenboard.cloud", recover_configs=True)

    assert settings.config_error is None
    assert config.stat().st_mtime_ns == mtime
    assert not broken_copies(config)


def test_empty_config_is_restored_to_production_without_quarantine(cloud_dirs):
    config = write_config(cloud_dirs, "custom-name", "")

    settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.config_error is None
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert not broken_copies(config)


def test_corrupted_config_is_restored_to_production_and_preserved(cloud_dirs, caplog):
    config = write_config(cloud_dirs, "custom-name", "{not json at all")

    caplog.set_level(logging.WARNING)
    AppSettings(provider_name="custom-name", recover_configs=True)

    kept = broken_copies(config)
    assert len(kept) == 1
    assert kept[0].read_text() == "{not json at all"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert "restored to production defaults" in caplog.text


def test_semantically_invalid_config_is_restored_to_production(cloud_dirs):
    config = write_config(cloud_dirs, "custom-name", '{"CLOUD_BASE_URL": null}')

    AppSettings(provider_name="custom-name", recover_configs=True)

    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert broken_copies(config)[0].read_text() == '{"CLOUD_BASE_URL": null}'


def test_malformed_cloud_url_is_restored_to_production(cloud_dirs):
    config = write_config(cloud_dirs, "custom-name", '{"CLOUD_BASE_URL": "http://["}')

    settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert broken_copies(config)[0].read_text() == '{"CLOUD_BASE_URL": "http://["}'


def test_missing_config_is_rebuilt_from_production_defaults(cloud_dirs):
    (cloud_dirs.providers / "custom-name").mkdir(parents=True)

    settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.config_error is None
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    rebuilt = json.loads((cloud_dirs.providers / "custom-name" / "wb-cloud-agent.conf").read_text())
    assert rebuilt["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert rebuilt["CLIENT_CERT_ENGINE_KEY"] == PACKAGED_DEFAULT["CLIENT_CERT_ENGINE_KEY"]


def test_recovery_uses_packaged_values_except_cloud_url(cloud_dirs):
    cloud_dirs.default.write_text(
        json.dumps({"CLOUD_BASE_URL": "https://wrong.example", "LOG_LEVEL": "DEBUG"}), encoding="utf-8"
    )
    config = write_config(cloud_dirs, "custom-name", "")

    settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert settings.log_level == "DEBUG"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"


def test_broken_packaged_default_uses_safe_builtins(cloud_dirs):
    cloud_dirs.default.write_text("", encoding="utf-8")
    config = write_config(cloud_dirs, "custom-name", "")

    settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    rebuilt = json.loads(config.read_text())
    assert rebuilt["CLIENT_CERT_ENGINE_KEY"] == AppSettings.client_cert_engine_key
    assert rebuilt["LOG_LEVEL"] == "INFO"


def test_config_recovery_uses_production_for_a_custom_named_provider(cloud_dirs):
    config = write_config(cloud_dirs, "mycloud", "{broken")

    settings = AppSettings(provider_name="mycloud", recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert config.read_text() != "{broken"


def test_recovery_keeps_original_when_broken_copy_cannot_be_saved(cloud_dirs):
    config = write_config(cloud_dirs, "custom-name", "{broken")

    with patch("wb.cloud_agent.settings.quarantine_broken_file", return_value=None):
        settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert config.read_text() == "{broken"


def test_recovery_keeps_original_when_rebuilt_config_cannot_be_written(cloud_dirs):
    config = write_config(cloud_dirs, "custom-name", "{broken")

    with patch("wb.cloud_agent.settings.write_to_file", side_effect=PermissionError("read-only")):
        settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.config_error is None
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert config.read_text() == "{broken"
    assert len(broken_copies(config)) == 1


def test_failed_recovery_does_not_duplicate_the_broken_copy(cloud_dirs):
    config = write_config(cloud_dirs, "custom-name", "{broken")

    with patch("wb.cloud_agent.settings.write_to_file", side_effect=PermissionError("read-only")):
        AppSettings(provider_name="custom-name", recover_configs=True)
        AppSettings(provider_name="custom-name", recover_configs=True)

    assert len(broken_copies(config)) == 1


def test_listing_providers_recovers_in_memory_only(cloud_dirs):
    config = write_config(cloud_dirs, "custom-name", "")

    providers = load_providers_data(["custom-name"])

    assert providers[0].config["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert config.read_text() == ""


def test_listing_uses_production_fallback_without_writing(cloud_dirs):
    write_config(cloud_dirs, "mycloud", "")
    write_config(cloud_dirs, "wirenboard.cloud", '{"CLOUD_BASE_URL": "https://wirenboard.cloud"}')

    providers = load_providers_data(["mycloud", "wirenboard.cloud"])

    assert [provider.name for provider in providers] == ["mycloud", "wirenboard.cloud"]
    assert providers[0].config["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
