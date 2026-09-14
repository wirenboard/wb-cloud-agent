import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wb.cloud_agent.commands import run_daemon
from wb.cloud_agent.constants import PRODUCTION_PROVIDER_NAME
from wb.cloud_agent.handlers.ping import CloudUnreachableError
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import ConfigError, ConfigReadError

PACKAGED_DEFAULT = {
    "LOG_LEVEL": "INFO",
    "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:02:C0:00",
    "CLOUD_BASE_URL": "https://wirenboard.cloud/",
}


@pytest.fixture(name="cloud_dirs")
def cloud_paths(tmp_path):
    providers = tmp_path / "etc" / "wb-cloud-agent" / "providers"
    default = tmp_path / "etc" / "wb-cloud-agent.conf"
    providers.mkdir(parents=True)
    default.write_text(json.dumps(PACKAGED_DEFAULT), encoding="utf-8")

    with (
        patch("wb.cloud_agent.settings.PROVIDERS_CONF_DIR", str(providers)),
        patch("wb.cloud_agent.settings.DEFAULT_PROVIDER_CONF_FILE", str(default)),
    ):
        yield providers, default


def write_config(providers: Path, provider: str, contents: str) -> Path:
    config = providers / provider / "wb-cloud-agent.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(contents, encoding="utf-8")
    return config


def broken_copies(config: Path) -> list[Path]:
    return sorted(config.parent.glob(f"{config.name}.broken-*"))


def test_missing_production_config_is_rebuilt(cloud_dirs):
    providers, _default = cloud_dirs
    (providers / PRODUCTION_PROVIDER_NAME).mkdir()

    settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    config = providers / PRODUCTION_PROVIDER_NAME / "wb-cloud-agent.conf"
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == PACKAGED_DEFAULT


@pytest.mark.parametrize("contents", ["", "{broken", "[]", "{}"])
def test_damaged_production_config_is_rebuilt(cloud_dirs, contents):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, contents)

    settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == PACKAGED_DEFAULT
    assert len(broken_copies(config)) == int(bool(contents))
    if contents:
        assert broken_copies(config)[0].read_text() == contents


def test_healthy_production_config_is_unchanged(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(
        providers,
        PRODUCTION_PROVIDER_NAME,
        json.dumps({"CLOUD_BASE_URL": "https://custom.example"}),
    )
    original = config.read_text()
    mtime = config.stat().st_mtime_ns

    settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://custom.example"
    assert config.read_text() == original
    assert config.stat().st_mtime_ns == mtime
    assert not broken_copies(config)


def test_custom_provider_is_not_rewritten(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, "custom", "{broken")

    with pytest.raises(ConfigError):
        AppSettings(provider_name="custom", recover_configs=True)

    assert config.read_text() == "{broken"
    assert not broken_copies(config)


def test_production_config_directory_is_not_rewritten(cloud_dirs):
    providers, _default = cloud_dirs
    config = providers / PRODUCTION_PROVIDER_NAME / "wb-cloud-agent.conf"
    config.parent.mkdir(parents=True)
    config.mkdir()

    with pytest.raises(ConfigReadError):
        AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert config.is_dir()
    assert not broken_copies(config)


def test_unreadable_production_config_is_rebuilt(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, json.dumps(PACKAGED_DEFAULT))

    def read_config(path, rebuild=None):
        del rebuild
        if path == config:
            raise ConfigReadError("cannot be read (permission denied)")
        return json.loads(path.read_text(encoding="utf-8"))

    with patch("wb.cloud_agent.settings.read_json_config", side_effect=read_config):
        settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == PACKAGED_DEFAULT
    assert config.stat().st_mode & 0o777 == 0o600
    assert not broken_copies(config)


def test_recovery_write_failure_preserves_damaged_config(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")

    with patch("wb.cloud_agent.settings.write_to_file", side_effect=OSError("read-only")):
        with pytest.raises(ConfigError):
            AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert config.read_text() == "{broken"
    assert broken_copies(config)[0].read_text() == "{broken"


def test_network_failure_does_not_recover_config():
    options = Namespace(provider_name=PRODUCTION_PROVIDER_NAME, broker=None)
    settings = MagicMock(cloud_base_url="https://wirenboard.cloud", ping_period_seconds=10)

    with (
        patch("wb.cloud_agent.commands.configure_app", return_value=settings),
        patch(
            "wb.cloud_agent.commands.wait_for_cloud_reachable",
            side_effect=CloudUnreachableError("offline"),
        ),
        patch("wb.cloud_agent.settings.recover_provider_config") as recover,
    ):
        assert run_daemon(options) == 1

    recover.assert_not_called()
