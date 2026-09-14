import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import PACKAGED_DEFAULT
from wb.cloud_agent.constants import (
    PRODUCTION_PROVIDER_IDENTITY,
    PROVIDER_IDENTITY_FILE_NAME,
)
from wb.cloud_agent.settings import (
    AppSettings,
    _packaged_default_config,
    default_client_cert_engine_key,
    generate_provider_config,
    load_providers_data,
)
from wb.cloud_agent.utils import ConfigRecoveryError


def write_config(dirs, provider, contents: str) -> Path:
    config = dirs.providers / provider / "wb-cloud-agent.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(contents, encoding="utf-8")
    return config


def broken_copies(config: Path) -> list[Path]:
    return sorted(config.parent.glob(f"{config.name}.broken-*"))


def mark_production(cloud_dirs, provider: str) -> None:
    marker = cloud_dirs.providers / provider / PROVIDER_IDENTITY_FILE_NAME
    marker.write_text(PRODUCTION_PROVIDER_IDENTITY, encoding="utf-8")


def test_healthy_config_is_never_rewritten(cloud_dirs):
    config = write_config(cloud_dirs, "wirenboard.cloud", '{"CLOUD_BASE_URL": "https://wirenboard.cloud"}')
    mtime = config.stat().st_mtime_ns
    mark_production(cloud_dirs, "wirenboard.cloud")

    settings = AppSettings(provider_name="wirenboard.cloud", recover_configs=True)

    assert settings.config_error is None
    assert config.stat().st_mtime_ns == mtime
    assert not broken_copies(config)
    assert (
        cloud_dirs.data / "wirenboard.cloud" / "wb-cloud-agent.conf.last-good"
    ).read_text() == config.read_text()


@pytest.mark.parametrize(
    "contents, reason, quarantined",
    [
        ("", "is empty", False),
        ("{not json", "is not valid JSON", True),
        ("[]", "is not a JSON object", True),
        ("{}", "has an invalid CLOUD_BASE_URL", True),
        ('{"CLOUD_BASE_URL": null}', "has an invalid CLOUD_BASE_URL", True),
        ('{"CLOUD_BASE_URL": "ftp://example.com"}', "has an invalid CLOUD_BASE_URL", True),
    ],
)
def test_damaged_config_is_rebuilt_and_logs_reason(cloud_dirs, caplog, contents, reason, quarantined):
    config = write_config(cloud_dirs, "provider", contents)
    mark_production(cloud_dirs, "provider")
    caplog.set_level(logging.WARNING)

    settings = AppSettings(provider_name="provider", recover_configs=True)

    assert settings.config_error is None
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert reason in caplog.text
    assert len(broken_copies(config)) == int(quarantined)
    if quarantined:
        assert broken_copies(config)[0].read_text() == contents


def test_undecodable_config_is_rebuilt_and_quarantined(cloud_dirs, caplog):
    config = write_config(cloud_dirs, "provider", "")
    mark_production(cloud_dirs, "provider")
    config.write_bytes(b"\xff\xfe")
    caplog.set_level(logging.WARNING)

    AppSettings(provider_name="provider", recover_configs=True)

    assert config.read_bytes() == json.dumps(PACKAGED_DEFAULT, indent=4).encode()
    assert broken_copies(config)[0].read_bytes() == b"\xff\xfe"
    assert "is not valid JSON" in caplog.text


def test_missing_config_is_rebuilt_when_provider_directory_exists(cloud_dirs):
    provider_dir = cloud_dirs.providers / "provider"
    provider_dir.mkdir(parents=True)
    mark_production(cloud_dirs, "provider")

    settings = AppSettings(provider_name="provider", recover_configs=True)

    config = provider_dir / "wb-cloud-agent.conf"
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == PACKAGED_DEFAULT


def test_custom_provider_is_not_redirected_without_last_good(cloud_dirs, caplog):
    cloud_dirs.default.write_text(
        json.dumps(
            {
                "CLOUD_BASE_URL": "http://on-premise.example:8080/base",
                "LOG_LEVEL": "DEBUG",
                "OPTIONAL": ["kept"],
            }
        ),
        encoding="utf-8",
    )
    config = write_config(cloud_dirs, "custom-name", "{broken")
    caplog.set_level(logging.WARNING)

    settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.config_unavailable
    assert config.read_text() == "{broken"
    assert not broken_copies(config)
    assert "no safe recovery source" in caplog.text


def test_custom_provider_recovers_verbatim_from_last_good(cloud_dirs):
    config = write_config(
        cloud_dirs,
        "custom-name",
        '{\n  "CLOUD_BASE_URL": "http://on-premise.example:8080/base",\n  "OPTIONAL": ["kept"]\n}\n',
    )
    expected = config.read_text()

    AppSettings(provider_name="custom-name", recover_configs=True)
    config.write_text("{broken", encoding="utf-8")

    settings = AppSettings(provider_name="custom-name", recover_configs=True)

    assert settings.cloud_base_url == "http://on-premise.example:8080/base"
    assert config.read_text() == expected
    assert broken_copies(config)[0].read_text() == "{broken"


def test_explicit_provider_replacement_discards_old_recovery_identity(cloud_dirs):
    config = write_config(cloud_dirs, "provider", '{"CLOUD_BASE_URL": "https://old.example"}')
    mark_production(cloud_dirs, "provider")
    last_good = cloud_dirs.data / "provider" / "wb-cloud-agent.conf.last-good"
    last_good.parent.mkdir(parents=True)
    last_good.write_text(config.read_text(), encoding="utf-8")

    generate_provider_config("provider", "https://new.example")

    assert not (config.parent / PROVIDER_IDENTITY_FILE_NAME).exists()
    assert json.loads(last_good.read_text())["CLOUD_BASE_URL"] == "https://new.example"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://new.example"


def test_broken_packaged_default_uses_builtins(cloud_dirs):
    cloud_dirs.default.write_text("", encoding="utf-8")
    config = write_config(cloud_dirs, "provider", "")
    mark_production(cloud_dirs, "provider")

    settings = AppSettings(provider_name="provider", recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == {
        "LOG_LEVEL": "INFO",
        "CLIENT_CERT_ENGINE_KEY": AppSettings.client_cert_engine_key,
        "CLOUD_BASE_URL": "https://wirenboard.cloud",
    }


def test_contactless_board_uses_the_wb6_certificate_key(monkeypatch):
    result = type("Result", (), {"returncode": 0, "stdout": "ATECCx08:00:04:C0:00"})()
    monkeypatch.setattr("wb.cloud_agent.settings.subprocess.run", lambda *args, **kwargs: result)

    assert default_client_cert_engine_key() == "ATECCx08:00:04:C0:00"


def test_packaged_defaults_use_the_contactless_board_key(cloud_dirs, monkeypatch):
    result = type("Result", (), {"returncode": 0, "stdout": "ATECCx08:00:04:C0:00"})()
    monkeypatch.setattr("wb.cloud_agent.settings.subprocess.run", lambda *args, **kwargs: result)
    cloud_dirs.default.write_text(json.dumps({"CLOUD_BASE_URL": "https://example.com"}))

    assert _packaged_default_config()["CLIENT_CERT_ENGINE_KEY"] == "ATECCx08:00:04:C0:00"


def test_unreadable_config_is_left_alone_and_stops_daemon_recovery(cloud_dirs, caplog):
    config = write_config(cloud_dirs, "provider", "")
    config.unlink()
    config.mkdir()
    caplog.set_level(logging.WARNING)

    with pytest.raises(ConfigRecoveryError):
        AppSettings(provider_name="provider", recover_configs=True)

    assert config.is_dir()
    assert not broken_copies(config)
    assert "leaving it unchanged" in caplog.text


def test_unreadable_config_falls_back_only_in_memory_for_management(cloud_dirs):
    config = write_config(cloud_dirs, "provider", "")
    config.unlink()
    config.mkdir()

    settings = AppSettings(provider_name="provider")
    providers = load_providers_data(["provider"])

    assert settings.config_error.startswith("cannot be read")
    assert providers[0].config == {}
    assert providers[0].damaged
    assert config.is_dir()


def test_recovery_does_not_recreate_a_removed_provider_directory(cloud_dirs, caplog):
    caplog.set_level(logging.WARNING)

    settings = AppSettings(provider_name="removed", recover_configs=True)

    assert settings.provider_removed
    assert not (cloud_dirs.providers / "removed").exists()
    assert "provider directory" in caplog.text


def test_failed_recovery_does_not_duplicate_broken_copy(cloud_dirs):
    config = write_config(cloud_dirs, "provider", "{broken")
    mark_production(cloud_dirs, "provider")

    with patch("wb.cloud_agent.settings.write_to_file", side_effect=PermissionError("read-only")):
        with pytest.raises(ConfigRecoveryError):
            AppSettings(provider_name="provider", recover_configs=True)
        with pytest.raises(ConfigRecoveryError):
            AppSettings(provider_name="provider", recover_configs=True)

    assert len(broken_copies(config)) == 1
    assert config.read_text() == "{broken"


def test_listing_recovers_in_memory_without_writing_or_creating_dirs(cloud_dirs):
    write_config(cloud_dirs, "broken", "")

    providers = load_providers_data(["broken", "missing"])

    assert [provider.name for provider in providers] == ["broken", "missing"]
    assert all(provider.damaged for provider in providers)
    assert all(provider.display_url == "Broken configuration" for provider in providers)
    assert not (cloud_dirs.providers / "missing").exists()
    assert (cloud_dirs.providers / "broken" / "wb-cloud-agent.conf").read_text() == ""
