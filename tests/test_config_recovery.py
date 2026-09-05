import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import PACKAGED_DEFAULT
from wb.cloud_agent.settings import (
    AppSettings,
    load_providers_data,
    looks_like_cloud_host,
    save_last_good_config,
)


def write_config(dirs, provider, contents: str) -> Path:
    config = dirs.providers / provider / "wb-cloud-agent.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(contents, encoding="utf-8")
    return config


def write_last_good(dirs, provider, contents: str) -> Path:
    last_good = dirs.data / provider / "wb-cloud-agent.conf.last-good"
    last_good.parent.mkdir(parents=True, exist_ok=True)
    last_good.write_text(contents, encoding="utf-8")
    return last_good


def broken_copies(config: Path) -> list:
    return sorted(config.parent.glob(f"{config.name}.broken-*"))


def test_healthy_config_is_never_rewritten(cloud_dirs):
    config = write_config(cloud_dirs, "wirenboard.cloud", '{"CLOUD_BASE_URL": "https://wirenboard.cloud"}')
    mtime = config.stat().st_mtime_ns

    settings = AppSettings(provider_name="wirenboard.cloud", recover_configs=True)

    assert settings.config_error is None
    assert config.stat().st_mtime_ns == mtime
    assert not broken_copies(config)


def test_empty_config_restored_from_last_good(cloud_dirs):
    config = write_config(cloud_dirs, "acme", "")
    write_last_good(cloud_dirs, "acme", '{"CLOUD_BASE_URL": "http://cloud.acme.local:8080"}')

    settings = AppSettings(provider_name="acme", recover_configs=True)

    assert settings.config_error is None
    assert settings.cloud_base_url == "http://cloud.acme.local:8080"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "http://cloud.acme.local:8080"
    assert not broken_copies(config), "an empty file preserves nothing and must not be kept"


def test_corrupted_config_is_quarantined_before_the_rebuild(cloud_dirs):
    config = write_config(cloud_dirs, "acme", "{not json at all")
    write_last_good(cloud_dirs, "acme", '{"CLOUD_BASE_URL": "https://cloud.acme.local"}')

    AppSettings(provider_name="acme", recover_configs=True)

    kept = broken_copies(config)
    assert len(kept) == 1
    assert kept[0].read_text() == "{not json at all"
    assert json.loads(config.read_text())["CLOUD_BASE_URL"] == "https://cloud.acme.local"


def test_missing_config_rebuilt_from_packaged_default(cloud_dirs):
    (cloud_dirs.providers / "wirenboard.cloud").mkdir(parents=True)

    settings = AppSettings(provider_name="wirenboard.cloud", recover_configs=True)

    assert settings.config_error is None
    assert settings.cloud_base_url == "https://wirenboard.cloud"
    rebuilt = json.loads((cloud_dirs.providers / "wirenboard.cloud" / "wb-cloud-agent.conf").read_text())
    assert rebuilt["CLIENT_CERT_ENGINE_KEY"] == PACKAGED_DEFAULT["CLIENT_CERT_ENGINE_KEY"]


def test_broken_packaged_default_keeps_the_url_from_the_directory_name(cloud_dirs):
    cloud_dirs.default.write_text("", encoding="utf-8")
    config = write_config(cloud_dirs, "cloud-staging.wirenboard.com", "")

    settings = AppSettings(provider_name="cloud-staging.wirenboard.com", recover_configs=True)

    assert settings.cloud_base_url == "https://cloud-staging.wirenboard.com"
    assert json.loads(config.read_text())["CLIENT_CERT_ENGINE_KEY"] == AppSettings.client_cert_engine_key


def test_custom_named_provider_is_not_repointed(cloud_dirs):
    config = write_config(cloud_dirs, "mycloud", "{broken")

    settings = AppSettings(provider_name="mycloud", recover_configs=True)

    assert settings.config_error is not None
    assert config.read_text() == "{broken", "the only copy of the real URL must survive"
    assert not broken_copies(config)


def test_unreadable_config_is_reported_not_rebuilt(cloud_dirs):
    write_config(cloud_dirs, "mycloud", '{"CLOUD_BASE_URL": "https://mycloud"}')

    with patch.object(Path, "read_text", side_effect=PermissionError("no access")):
        settings = AppSettings(provider_name="mycloud", recover_configs=True)

    assert "cannot be read" in settings.config_error


def test_config_error_clears_once_the_file_is_restored(cloud_dirs):
    config = write_config(cloud_dirs, "mycloud", "")
    settings = AppSettings(provider_name="mycloud", recover_configs=True)
    assert settings.config_error is not None

    config.write_text('{"CLOUD_BASE_URL": "https://cloud.acme.local"}', encoding="utf-8")
    settings.reload_config()

    assert settings.config_error is None
    assert settings.cloud_base_url == "https://cloud.acme.local"


def test_listing_providers_recovers_in_memory_only(cloud_dirs):
    config = write_config(cloud_dirs, "wirenboard.cloud", "")

    providers = load_providers_data(["wirenboard.cloud"])

    assert providers[0].config["CLOUD_BASE_URL"] == "https://wirenboard.cloud"
    assert config.read_text() == "", "listing providers must not create or repair files"


def test_listing_skips_a_provider_that_cannot_be_described(cloud_dirs):
    write_config(cloud_dirs, "mycloud", "")
    write_config(cloud_dirs, "wirenboard.cloud", '{"CLOUD_BASE_URL": "https://wirenboard.cloud"}')

    providers = load_providers_data(["mycloud", "wirenboard.cloud"])

    assert [provider.name for provider in providers] == ["wirenboard.cloud"]


def test_save_last_good_config_copies_verbatim(cloud_dirs):
    contents = '{"CLOUD_BASE_URL": "http://cloud.acme.local:8080"}'
    write_config(cloud_dirs, "acme", contents)

    save_last_good_config("acme")

    assert (cloud_dirs.data / "acme" / "wb-cloud-agent.conf.last-good").read_text() == contents


def test_save_last_good_config_skips_an_unchanged_copy(cloud_dirs):
    write_config(cloud_dirs, "acme", '{"CLOUD_BASE_URL": "https://cloud.acme.local"}')
    save_last_good_config("acme")
    last_good = cloud_dirs.data / "acme" / "wb-cloud-agent.conf.last-good"
    mtime = last_good.stat().st_mtime_ns

    save_last_good_config("acme")

    assert last_good.stat().st_mtime_ns == mtime


def test_save_last_good_config_survives_a_missing_config(cloud_dirs):
    save_last_good_config("acme")

    assert not (cloud_dirs.data / "acme").exists()


@pytest.mark.parametrize(
    "provider_name, expected",
    [
        ("wirenboard.cloud", True),
        ("cloud.acme.local:8443", True),
        ("mycloud", False),
        ("mycloud:8443", False),
        ("cloud.acme.local:https", False),
        (".cloud", False),
    ],
)
def test_looks_like_cloud_host(provider_name, expected):
    assert looks_like_cloud_host(provider_name) is expected
