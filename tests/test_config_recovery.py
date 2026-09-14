import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from wb.cloud_agent.commands import run_daemon
from wb.cloud_agent.constants import (
    NOTCONFIGURED_EXIT_CODE,
    PRODUCTION_PROVIDER_NAME,
    WB6_DEVICE_TREE_COMPATIBLE,
    WB6_ENGINE_KEY_PREFIX,
)
from wb.cloud_agent.handlers.ping import CloudUnreachableError
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import (
    ConfigError,
    ConfigReadError,
    fix_engine_key,
    write_to_file,
)

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


def test_network_failure_does_not_recover_config(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(
        providers,
        PRODUCTION_PROVIDER_NAME,
        json.dumps({"CLOUD_BASE_URL": "https://custom.example", "PING_PERIOD_SECONDS": 1}),
    )
    original = config.read_text()
    options = Namespace(provider_name=PRODUCTION_PROVIDER_NAME, broker=None)

    with patch(
        "wb.cloud_agent.commands.wait_for_cloud_reachable",
        side_effect=CloudUnreachableError("offline"),
    ):
        assert run_daemon(options) == 1

    assert config.read_text() == original
    assert not broken_copies(config)


def test_run_daemon_reports_notconfigured_for_custom_provider(cloud_dirs):
    providers, _default = cloud_dirs
    write_config(providers, "custom", "{broken")
    options = Namespace(provider_name="custom", broker=None)

    with patch("wb.cloud_agent.commands.wait_for_cloud_reachable") as wait:
        assert run_daemon(options) == NOTCONFIGURED_EXIT_CODE

    wait.assert_not_called()


def test_only_latest_broken_copy_is_kept(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{first")
    AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)
    config.write_text("{second", encoding="utf-8")

    AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    copies = broken_copies(config)
    assert len(copies) == 1
    assert copies[0].read_text() == "{second"


def test_recovered_config_gets_local_engine_key(cloud_dirs, tmp_path):
    providers, _default = cloud_dirs
    (providers / PRODUCTION_PROVIDER_NAME).mkdir()
    compatible = tmp_path / "compatible"
    compatible.write_bytes(
        b"contactless,imx6ul-wirenboard610\0" + WB6_DEVICE_TREE_COMPATIBLE.encode() + b"\0"
    )

    with patch("wb.cloud_agent.utils.DEVICE_TREE_COMPATIBLE_PATH", str(compatible)):
        settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    config = providers / PRODUCTION_PROVIDER_NAME / "wb-cloud-agent.conf"
    assert settings.client_cert_engine_key == f"{WB6_ENGINE_KEY_PREFIX}:C0:00"
    assert json.loads(config.read_text())["CLIENT_CERT_ENGINE_KEY"] == f"{WB6_ENGINE_KEY_PREFIX}:C0:00"


def test_fix_engine_key_without_device_tree_keeps_default(tmp_path):
    with patch("wb.cloud_agent.utils.DEVICE_TREE_COMPATIBLE_PATH", str(tmp_path / "missing")):
        assert fix_engine_key(dict(PACKAGED_DEFAULT)) == PACKAGED_DEFAULT
        assert fix_engine_key({"CLOUD_BASE_URL": "x"}) == {"CLOUD_BASE_URL": "x"}


def test_write_to_file_permissions(tmp_path):
    fresh = tmp_path / "fresh.conf"
    write_to_file(fresh, "{}")
    assert fresh.stat().st_mode & 0o777 == 0o644

    existing = tmp_path / "existing.conf"
    existing.write_text("old", encoding="utf-8")
    existing.chmod(0o640)
    write_to_file(existing, "new")
    assert existing.read_text() == "new"
    assert existing.stat().st_mode & 0o777 == 0o640

    explicit = tmp_path / "explicit.conf"
    write_to_file(explicit, "{}", mode=0o600)
    assert explicit.stat().st_mode & 0o777 == 0o600
