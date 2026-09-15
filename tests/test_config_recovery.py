import json
import logging
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from wb.cloud_agent.commands import del_provider, run_daemon
from wb.cloud_agent.constants import (
    NOTCONFIGURED_EXIT_CODE,
    PRODUCTION_PROVIDER_NAME,
    WB6_DEVICE_TREE_COMPATIBLE,
    WB6_ENGINE_KEY_PREFIX,
)
from wb.cloud_agent.handlers.ping import CloudUnreachableError
from wb.cloud_agent.main import main
from wb.cloud_agent.settings import (
    AppSettings,
    configure_app,
    generate_provider_config,
    load_providers_data,
)
from wb.cloud_agent.utils import (
    ConfigError,
    ConfigReadError,
    local_engine_key,
    with_local_engine_key,
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


@pytest.mark.parametrize("contents", ["", "{broken", "[]", '{"CLOUD_BASE_URL": "ftp://nope"}'])
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


def test_unreadable_production_config_is_preserved_not_destroyed(cloud_dirs):
    """A config we cannot read is moved aside by rename, so its content survives."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, '{"CLOUD_BASE_URL": "https://mine.example"}')
    original = config.read_text()

    def unreadable(path):
        if path == config:
            raise ConfigReadError("cannot be read (permission denied)")
        return json.loads(path.read_text(encoding="utf-8"))

    with patch("wb.cloud_agent.settings.read_json_config", side_effect=unreadable):
        settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == PACKAGED_DEFAULT
    assert broken_copies(config)[0].read_text() == original


def test_recovery_write_failure_preserves_damaged_config(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")

    with patch("wb.cloud_agent.settings.write_to_file", side_effect=OSError("read-only")):
        with pytest.raises(ConfigError):
            AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert broken_copies(config)[0].read_text() == "{broken"


def test_quarantine_failure_leaves_config_alone(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")

    with patch("wb.cloud_agent.settings.quarantine_broken_file", side_effect=OSError("read-only")):
        with pytest.raises(ConfigError):
            AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert config.read_text() == "{broken"


def test_oldest_and_newest_broken_copies_are_kept(cloud_dirs):
    """The first copy holds the operator's own settings; the last one describes the latest failure."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, '{"CLOUD_BASE_URL": "https://mine.example"')
    for damage in ("{second", "{third"):
        AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)
        config.write_text(damage, encoding="utf-8")
    AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    kept = [copy.read_text() for copy in broken_copies(config)]
    assert kept == ['{"CLOUD_BASE_URL": "https://mine.example"', "{third"]


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


def test_main_turns_unusable_config_into_systemd_status(cloud_dirs):
    """A custom provider with a broken config stops the agent, but does not crash it."""
    providers, _default = cloud_dirs
    write_config(providers, "custom", "{broken")

    with patch(
        "wb.cloud_agent.main.parse_args",
        return_value=Namespace(func=run_daemon, provider_name="custom", broker=None),
    ):
        assert main() == NOTCONFIGURED_EXIT_CODE


@pytest.mark.parametrize(
    "extra",
    [
        {"REQUEST_PERIOD_SECONDS": 10.5},
        {"PING_PERIOD_SECONDS": "30"},
        {"METRICS_LOG_ENABLED": "yes"},
        {"MY_OWN_FLAG": True},
        {"BROKER_URL": ""},
    ],
)
def test_configs_accepted_before_keep_working(cloud_dirs, extra):
    """Self-healing must not invent new reasons to refuse a config that used to work."""
    providers, _default = cloud_dirs
    config = write_config(providers, "custom", json.dumps({"CLOUD_BASE_URL": "https://my.cloud", **extra}))
    original = config.read_text()

    settings = configure_app(provider_name="custom")

    assert settings.cloud_base_url == "https://my.cloud"
    assert config.read_text() == original


def test_invalid_log_level_does_not_stop_the_agent(cloud_dirs):
    providers, _default = cloud_dirs
    write_config(providers, "custom", json.dumps({"CLOUD_BASE_URL": "https://my.cloud", "LOG_LEVEL": 10}))

    settings = configure_app(provider_name="custom")

    assert settings.cloud_base_url == "https://my.cloud"


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


def test_healthy_config_with_stale_engine_key_is_corrected_in_memory(cloud_dirs, tmp_path):
    """check-certs.sh no longer rewrites the file, so the agent corrects the key as it loads it."""
    providers, _default = cloud_dirs
    config = write_config(
        providers,
        PRODUCTION_PROVIDER_NAME,
        json.dumps({"CLOUD_BASE_URL": "https://wirenboard.cloud", **PACKAGED_DEFAULT}),
    )
    original = config.read_text()
    compatible = tmp_path / "compatible"
    compatible.write_bytes(WB6_DEVICE_TREE_COMPATIBLE.encode() + b"\0")

    with patch("wb.cloud_agent.utils.DEVICE_TREE_COMPATIBLE_PATH", str(compatible)):
        settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.client_cert_engine_key == f"{WB6_ENGINE_KEY_PREFIX}:C0:00"
    assert config.read_text() == original


def test_engine_key_without_device_tree_keeps_default(tmp_path):
    with patch("wb.cloud_agent.utils.DEVICE_TREE_COMPATIBLE_PATH", str(tmp_path / "missing")):
        assert local_engine_key("ATECCx08:00:02:C0:00") == "ATECCx08:00:02:C0:00"
        assert local_engine_key(None) is None


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


def test_config_without_base_url_is_left_alone(cloud_dirs):
    """Configs predating 1.6.0 have no CLOUD_BASE_URL and run off the built-in default."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, json.dumps({"LOG_LEVEL": "INFO"}))
    original = config.read_text()

    settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert config.read_text() == original
    assert not broken_copies(config)


def test_provider_without_base_url_can_still_be_deleted(cloud_dirs):
    """del-provider must not depend on the config being complete."""
    providers, _default = cloud_dirs
    write_config(providers, "old-provider", json.dumps({"LOG_LEVEL": "INFO"}))

    with (
        patch("wb.cloud_agent.commands.MQTTCloudAgent"),
        patch("wb.cloud_agent.commands.get_provider_names", return_value=["old-provider"]),
        patch("wb.cloud_agent.commands.stop_services_and_del_configs") as stop,
    ):
        assert del_provider(Namespace(provider_name="old-provider")) == 0

    stop.assert_called_once()


def test_recovery_works_with_pre_1_6_packaged_default(cloud_dirs):
    """/etc/wb-cloud-agent.conf from 1.5.x has no CLOUD_BASE_URL and is a modified conffile."""
    providers, default = cloud_dirs
    default.write_text(
        json.dumps({"LOG_LEVEL": "INFO", "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:04:C0:00"}),
        encoding="utf-8",
    )
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")

    with patch("wb.cloud_agent.utils.local_engine_key_prefix", return_value="ATECCx08:00:02"):
        settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    restored = json.loads(config.read_text())
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert restored["LOG_LEVEL"] == "INFO"
    assert "CLOUD_BASE_URL" not in restored
    assert broken_copies(config)[0].read_text() == "{broken"


def test_add_provider_works_with_pre_1_6_packaged_default(cloud_dirs):
    providers, default = cloud_dirs
    default.write_text(json.dumps({"LOG_LEVEL": "INFO"}), encoding="utf-8")

    generate_provider_config("my.cloud", "https://my.cloud")

    written = json.loads((providers / "my.cloud" / "wb-cloud-agent.conf").read_text())
    assert written["CLOUD_BASE_URL"] == "https://my.cloud"


def test_info_logging_survives_a_recovery(cloud_dirs):
    """The recovery warning must not leave the root logger stuck at WARNING."""
    providers, _default = cloud_dirs
    write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.NOTSET)

    configure_app(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert logging.getLogger().isEnabledFor(logging.INFO)


def test_cli_repairs_a_damaged_production_config(cloud_dirs):
    """CLOUD-592: the user met the broken config through `wb-cloud-agent`, not through the daemon."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "")

    providers_data = load_providers_data([PRODUCTION_PROVIDER_NAME])

    assert providers_data[0].config["CLOUD_BASE_URL"] == "https://wirenboard.cloud/"
    assert json.loads(config.read_text()) == PACKAGED_DEFAULT


def test_recovery_falls_back_to_built_in_values(cloud_dirs):
    """v.romanov on CLOUD-599: on fallback, land on our default production cloud."""
    providers, default = cloud_dirs
    default.write_text("", encoding="utf-8")
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")

    settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert broken_copies(config)[0].read_text() == "{broken"


def test_add_provider_survives_a_damaged_packaged_default(cloud_dirs):
    providers, default = cloud_dirs
    default.write_text("{broken", encoding="utf-8")

    generate_provider_config("my.cloud", "https://my.cloud")

    written = json.loads((providers / "my.cloud" / "wb-cloud-agent.conf").read_text())
    assert written["CLOUD_BASE_URL"] == "https://my.cloud"


def test_with_local_engine_key_does_not_touch_the_caller_dict():
    original = {"CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:09:C0:00", "LOG_LEVEL": "INFO"}
    with patch("wb.cloud_agent.utils.local_engine_key_prefix", return_value="ATECCx08:00:02"):
        updated = with_local_engine_key(original)

    assert original["CLIENT_CERT_ENGINE_KEY"] == "ATECCx08:00:09:C0:00"
    assert updated["CLIENT_CERT_ENGINE_KEY"] == "ATECCx08:00:02:C0:00"


def test_agent_url_is_always_derived_from_the_base_url():
    """Keeping a literal agent URL alongside the base one only lets the two drift apart."""
    assert not hasattr(AppSettings, "cloud_agent_url")

    settings = AppSettings(provider_name="", skip_conf_file=True, cloud_base_url="https://on-premise.example")

    assert settings.cloud_agent_url == "https://agent.on-premise.example/api-agent/v1/"


def test_built_in_url_follows_the_production_provider_name():
    assert AppSettings.cloud_base_url == f"https://{PRODUCTION_PROVIDER_NAME}"
