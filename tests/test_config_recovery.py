import json
import logging
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from wb.cloud_agent.commands import del_provider, run_daemon
from wb.cloud_agent.constants import (
    NOCONNECT_LINK,
    NOTCONFIGURED_EXIT_CODE,
    PRODUCTION_PROVIDER_NAME,
    UNKNOWN_LINK,
    WB6_DEVICE_TREE_COMPATIBLE,
    WB6_ENGINE_KEY_PREFIX,
)
from wb.cloud_agent.handlers.ping import CloudUnreachableError
from wb.cloud_agent.main import main
from wb.cloud_agent.settings import (
    AppSettings,
    Provider,
    configure_app,
    generate_provider_config,
    load_providers_data,
)
from wb.cloud_agent.utils import ConfigError, local_engine_key, write_to_file

UNIT_FILE = Path(__file__).resolve().parents[1] / "debian" / "wb-cloud-agent.wb-cloud-agent@.service"

BUILT_IN = {
    "LOG_LEVEL": "INFO",
    "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:02:C0:00",
    "CLOUD_BASE_URL": "https://wirenboard.cloud",
}


@pytest.fixture(autouse=True)
def isolated_logging():
    """setup_log() configures the root logger, and that state leaks between tests."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers.clear()
    root.setLevel(logging.WARNING)  # the default a fresh process starts with
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture(name="cloud_dirs")
def cloud_paths(tmp_path):
    providers = tmp_path / "etc" / "wb-cloud-agent" / "providers"
    default = tmp_path / "etc" / "wb-cloud-agent.conf"
    providers.mkdir(parents=True)

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
    assert json.loads(config.read_text()) == BUILT_IN


@pytest.mark.parametrize("contents", ["", "{broken", "[]"])
def test_damaged_production_config_is_rebuilt(cloud_dirs, contents):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, contents)

    settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == BUILT_IN
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

    with pytest.raises(ConfigError, match="not a regular file"):
        AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert config.is_dir()
    assert not broken_copies(config)


def test_unreadable_production_config_is_preserved_not_destroyed(cloud_dirs):
    """Hardlinking needs no read access, so a config we cannot read is kept, not lost."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, '{"CLOUD_BASE_URL": "https://mine.example"}')
    original = config.read_text()

    def unreadable(path):
        if path == config:
            raise ConfigError("cannot be read (permission denied)")
        return json.loads(path.read_text(encoding="utf-8"))

    with patch("wb.cloud_agent.settings.read_json_config", side_effect=unreadable):
        settings = AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert settings.cloud_base_url == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == BUILT_IN
    assert broken_copies(config)[0].read_text() == original


@pytest.mark.parametrize("failing_step", ["stage_file", "quarantine_broken_file", "commit_staged"])
def test_a_failed_recovery_never_leaves_the_directory_without_a_config(cloud_dirs, failing_step):
    """A full disk must not turn a damaged config into no config at all."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")

    with patch(f"wb.cloud_agent.settings.{failing_step}", side_effect=OSError("no space left")):
        with pytest.raises(ConfigError):
            AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert config.read_text() == "{broken"
    assert not list(config.parent.glob(f".{config.name}.tmp-*"))


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
        assert main() == 6


@pytest.mark.skipif(not UNIT_FILE.is_file(), reason="the packaging tree is not part of the built package")
def test_the_unit_stops_retrying_on_our_exit_status():
    """The literal above only means anything if the unit keys on the same one."""
    assert f"RestartPreventExitStatus={NOTCONFIGURED_EXIT_CODE}" in UNIT_FILE.read_text()


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
        json.dumps(BUILT_IN),
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


@pytest.mark.parametrize(
    ("contents", "unbound"),
    [(json.dumps({"LOG_LEVEL": "INFO"}), True), ("{broken", False), ("", False)],
)
def test_a_provider_can_be_deleted_whatever_its_config(cloud_dirs, contents, unbound):
    """A damaged config hides the provider's cloud, so the unbind must not go to the built-in one."""
    providers, _default = cloud_dirs
    write_config(providers, "doomed", contents)

    with (
        patch("wb.cloud_agent.commands.MQTTCloudAgent"),
        patch("wb.cloud_agent.commands.get_provider_names", return_value=["doomed"]),
        patch("wb.cloud_agent.services.lifecycle.read_activation_link", return_value=UNKNOWN_LINK),
        patch("wb.cloud_agent.services.lifecycle.stop_and_disable_service") as stop,
        patch("wb.cloud_agent.services.lifecycle.delete_provider_config"),
        patch("wb.cloud_agent.services.lifecycle.event_delete_controller", return_value=0) as unbind,
    ):
        assert del_provider(Namespace(provider_name="doomed")) == 0

    assert stop.call_count == 3
    assert unbind.called is unbound


@pytest.mark.parametrize("packaged_default", [json.dumps({"LOG_LEVEL": "INFO"}), "{broken", ""])
def test_add_provider_survives_any_packaged_default(cloud_dirs, packaged_default):
    providers, default = cloud_dirs
    default.write_text(packaged_default, encoding="utf-8")

    generate_provider_config("my.cloud", "https://my.cloud")

    written = json.loads((providers / "my.cloud" / "wb-cloud-agent.conf").read_text())
    assert written["CLOUD_BASE_URL"] == "https://my.cloud"


def test_info_logging_survives_a_recovery(cloud_dirs):
    """The recovery warning must not leave the root logger stuck at WARNING."""
    providers, _default = cloud_dirs
    write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")

    configure_app(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert logging.getLogger().isEnabledFor(logging.INFO)


def test_cli_repairs_a_damaged_production_config(cloud_dirs):
    """CLOUD-592: the user met the broken config through `wb-cloud-agent`, not through the daemon."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "")

    providers_data = load_providers_data([PRODUCTION_PROVIDER_NAME])

    assert providers_data[0].config["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert json.loads(config.read_text()) == BUILT_IN


def test_agent_url_is_always_derived_from_the_base_url():
    settings = AppSettings(provider_name="", skip_conf_file=True, cloud_base_url="https://on-premise.example")

    assert settings.cloud_agent_url == "https://agent.on-premise.example/api-agent/v1/"


def test_quarantine_slots_do_not_depend_on_the_clock(cloud_dirs):
    """These controllers boot with a wrong clock, so copies cannot be ordered by timestamp."""
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "конфиг оператора{")
    for damage in ("вторая поломка{", "третья поломка{"):
        AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)
        config.write_text(damage, encoding="utf-8")
    AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert config.with_name(f"{config.name}.broken-first").read_text() == "конфиг оператора{"
    assert config.with_name(f"{config.name}.broken-last").read_text() == "третья поломка{"
    assert len(broken_copies(config)) == 2


def test_listing_a_healthy_config_changes_nothing(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, json.dumps(BUILT_IN))
    before = config.stat().st_mtime_ns

    load_providers_data([PRODUCTION_PROVIDER_NAME])

    assert config.stat().st_mtime_ns == before
    assert sorted(path.name for path in config.parent.iterdir()) == [config.name]


def test_quarantine_follows_a_symlinked_config(cloud_dirs, tmp_path):
    """wb-configs moves configs to /mnt/data and leaves a symlink behind."""
    providers, _default = cloud_dirs
    real = tmp_path / "persisted.conf"
    real.write_text("конфиг оператора{", encoding="utf-8")
    config = providers / PRODUCTION_PROVIDER_NAME / "wb-cloud-agent.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.symlink_to(real)

    AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert config.is_symlink()
    assert json.loads(real.read_text()) == BUILT_IN
    assert real.with_name(f"{real.name}.broken-first").read_text() == "конфиг оператора{"


def test_a_provider_without_a_cloud_url_is_still_printable():
    """Configs written before 1.6.0 are left alone, so printing must cope with them."""
    provider = Provider(
        name=PRODUCTION_PROVIDER_NAME, config={"LOG_LEVEL": "INFO"}, activation_link=NOCONNECT_LINK
    )

    assert provider.display_url == "No connect to: https://wirenboard.cloud"


def test_a_power_cut_does_not_leave_temp_files_behind(cloud_dirs):
    providers, _default = cloud_dirs
    config = write_config(providers, PRODUCTION_PROVIDER_NAME, "{broken")
    orphan = config.parent / f".{config.name}.tmp-interrupted"
    orphan.write_text("half written", encoding="utf-8")

    AppSettings(provider_name=PRODUCTION_PROVIDER_NAME, recover_configs=True)

    assert not orphan.exists()
    assert not list(config.parent.glob(f".{config.name}.tmp-*"))
