import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse, urlunparse

from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.cloud_agent.constants import (
    APP_DATA_DIR,
    APP_DATA_PROVIDERS_DIR,
    CLOUD_AGENT_URL_POSTFIX,
    DEFAULT_PROVIDER_CONF_FILE,
    NOCONNECT_LINK,
    NOTCONFIGURED_EXIT_CODE,
    PRODUCTION_PROVIDER_NAME,
    PROVIDERS_CONF_DIR,
)
from wb.cloud_agent.utils import (
    ConfigError,
    ConfigReadError,
    config_recovery_lock,
    get_controller_url,
    local_engine_key,
    normalize_base_url,
    quarantine_broken_file,
    read_json_config,
    read_plaintext_config,
    with_local_engine_key,
    write_to_file,
)


def provider_config_path(provider_name: str) -> Path:
    return Path(PROVIDERS_CONF_DIR) / provider_name / "wb-cloud-agent.conf"


def _validate_provider_config(config: dict[str, Any]) -> None:
    """
    Check only what the agent cannot run without.

    Everything else is applied as written: a config the previous version accepted
    must keep working, and a self-healing agent should not invent new reasons to fail.
    """
    cloud_base_url = config.get("CLOUD_BASE_URL")
    if not isinstance(cloud_base_url, str) or not cloud_base_url.strip():
        raise ConfigError("has no usable CLOUD_BASE_URL")
    try:
        parsed = urlparse(cloud_base_url)
    except ValueError as exc:
        raise ConfigError("has an invalid CLOUD_BASE_URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError("has an invalid CLOUD_BASE_URL")


class AppSettings:  # pylint: disable=too-many-instance-attributes disable=too-few-public-methods
    """
    Simple settings configurator.

    To rewrite parameters just add them to wb-cloud-agent config.

    An example of config at /etc/wb-cloud-agent.conf:

    {
        "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:04:C0:00",
    }
    """

    provider_name: str

    skip_conf_file: bool = False
    recover_configs: bool = False

    log_level: str = "INFO"

    broker_url: str = DEFAULT_BROKER_URL

    client_cert_engine_key: str = "ATECCx08:00:02:C0:00"
    client_cert_file: str = f"{APP_DATA_DIR}/device_bundle.crt.pem"

    cloud_base_url: str = "https://wirenboard.cloud"
    cloud_agent_url: str = f"https://agent.wirenboard.cloud{CLOUD_AGENT_URL_POSTFIX}"
    request_period_seconds: int = 10
    ping_period_seconds: int = 10
    metrics_log_enabled: bool = True

    def __init__(self, /, **kwargs: dict[str, Any]) -> None:
        for key, val in kwargs.items():
            setattr(self, key, val)

        self.config_file: Path = provider_config_path(self.provider_name)
        self.frp_service: str = f"wb-cloud-agent-frpc@{self.provider_name}.service"
        self.metrics_service: str = f"wb-cloud-agent-metrics@{self.provider_name}.service"
        self.frp_config: Path = Path(f"{APP_DATA_PROVIDERS_DIR}/{self.provider_name}/frpc.conf")
        self.metrics_script: Path = Path(
            f"{APP_DATA_PROVIDERS_DIR}/{self.provider_name}/metrics_collector.py"
        )
        self.metrics_vars_config: Path = Path(
            f"{APP_DATA_PROVIDERS_DIR}/{self.provider_name}/metrics_collector.conf"
        )
        self.metrics_last_uid: Path = Path(f"{APP_DATA_PROVIDERS_DIR}/{self.provider_name}/metrics_last_uid")
        self.activation_link_config: Path = Path(
            f"{APP_DATA_PROVIDERS_DIR}/{self.provider_name}/activation_link.conf"
        )
        self.mqtt_prefix: str = f"/devices/system__wb-cloud-agent__{self.provider_name}"
        self.diag_archive: Path = Path("/tmp")

        if not self.skip_conf_file and (
            self.config_file.exists() or (self.recover_configs and self.config_file.parent.is_dir())
        ):
            self.apply_conf_file()

        # the packaged default names the WB7/WB8 bus; on WB6 the chip sits elsewhere
        self.client_cert_engine_key = local_engine_key(self.client_cert_engine_key)

        self.cloud_base_url = normalize_base_url(self.cloud_base_url)
        self.cloud_agent_url = self.base_url_to_agent_url(self.cloud_base_url)

    def apply_conf_file(self) -> None:
        try:
            conf = read_json_config(self.config_file)
            _validate_provider_config(conf)
        except ConfigError as exc:
            if not self.recover_configs:
                raise
            # a config that is not a regular file (a directory, say) is not ours to replace
            if isinstance(exc, ConfigReadError) and not self.config_file.is_file():
                raise
            conf = recover_provider_config(self.provider_name, str(exc))

        for key, val in conf.items():
            setattr(self, key.lower(), val)

    def base_url_to_agent_url(self, base_url: str) -> str:
        parsed = urlparse(normalize_base_url(base_url))
        netloc = f"agent.{parsed.netloc}"
        return urlunparse((parsed.scheme, netloc, CLOUD_AGENT_URL_POSTFIX, "", "", ""))


def configure_app(**kwargs: dict[str, Any]) -> AppSettings:
    """Raises ConfigError when the provider config is unusable; main() turns that into status 6."""
    settings = AppSettings(**kwargs)
    setup_log(settings)
    return settings


def setup_log(settings: AppSettings) -> None:
    level = settings.log_level
    numeric_level = getattr(logging, level.upper(), None) if isinstance(level, str) else None
    if not isinstance(numeric_level, int):
        logging.basicConfig(level=logging.INFO, encoding="utf-8", format="%(message)s")
        logging.warning("Invalid LOG_LEVEL %r in config, using INFO", level)
        return
    logging.basicConfig(level=numeric_level, encoding="utf-8", format="%(message)s")


def generate_provider_config(provider: str, base_url: str) -> None:
    conf = with_local_engine_key(_packaged_default_config())
    conf["CLOUD_BASE_URL"] = normalize_base_url(base_url)

    write_to_file(provider_config_path(provider), json.dumps(conf, indent=4))


def _packaged_default_config() -> dict[str, Any]:
    config = read_json_config(Path(DEFAULT_PROVIDER_CONF_FILE))
    _validate_provider_config(config)
    return config


def recover_provider_config(provider_name: str, reason: str) -> dict[str, Any]:
    if provider_name != PRODUCTION_PROVIDER_NAME:
        raise ConfigError(f"{reason}; recovery is limited to {PRODUCTION_PROVIDER_NAME}")

    config_path = provider_config_path(provider_name)
    if not config_path.parent.is_dir():
        raise ConfigError(f"{config_path} provider directory is missing")

    with config_recovery_lock(config_path):
        # another agent process may have fixed it while we waited for the lock
        try:
            current = read_json_config(config_path)
            _validate_provider_config(current)
            return current
        except ConfigError:
            pass

        recovered = with_local_engine_key(_packaged_default_config())
        try:
            # an empty file carries nothing worth keeping
            keep_broken = config_path.exists() and config_path.stat().st_size > 0
        except OSError as exc:
            raise ConfigError(f"cannot inspect broken config {config_path}: {exc}") from exc

        quarantined = None
        if keep_broken:
            try:
                quarantined = quarantine_broken_file(config_path)
            except OSError as exc:
                raise ConfigError(f"cannot move broken config {config_path} aside: {exc}") from exc

        try:
            write_to_file(config_path, json.dumps(recovered, indent=4), create_parent=False)
        except OSError as exc:
            raise ConfigError(f"cannot rewrite config {config_path}: {exc}") from exc

    logging.warning(
        "Config %s %s, restored from packaged defaults%s",
        config_path,
        reason,
        f", broken file kept as {quarantined.name}" if quarantined else "",
    )
    return recovered


def delete_provider_config(conf_path_prefix: str, provider: str) -> None:
    """Delete dir with config files"""

    path = Path(conf_path_prefix)
    target_dir = path / provider

    if target_dir.exists() and target_dir.is_dir():
        try:
            shutil.rmtree(target_dir)
        except FileNotFoundError:
            logging.debug("Dir %s does not exists", target_dir)
        except PermissionError:
            logging.debug("No have permission to delete dir %s", target_dir)
        except OSError as exc:
            logging.debug("Error %s on delete dir %s", str(exc), target_dir)
    else:
        logging.debug("Dir %s does not exists", target_dir)


def get_provider_names() -> list[str]:
    conf_path = Path(PROVIDERS_CONF_DIR)

    if conf_path.exists():
        return [d.name for d in conf_path.iterdir() if d.is_dir()]
    return []


@dataclass
class Provider:
    name: str
    config: dict[str, Union[str, int]]
    activation_link: Optional[str] = None

    @property
    def display_url(self) -> str:
        if self.activation_link and self.activation_link.startswith("http"):
            return self.activation_link

        if self.activation_link == NOCONNECT_LINK:
            return f"No connect to: {self.config['CLOUD_BASE_URL']}"

        return get_controller_url(self.config["CLOUD_BASE_URL"])


def load_providers_data(provider_names: list[str]) -> list[Provider]:
    """Load provider configs and actiovation links from files"""
    logging.debug("Load configs for provider_names = %s", provider_names)

    result = []
    for provider_name in provider_names:
        config_path = Path(f"{PROVIDERS_CONF_DIR}/{provider_name}/wb-cloud-agent.conf")
        activation_path = Path(f"{APP_DATA_PROVIDERS_DIR}/{provider_name}/activation_link.conf")

        try:
            provider_config = read_json_config(config_path)
        except ConfigError as exc:
            print(f"Config {config_path} {exc}")
            sys.exit(NOTCONFIGURED_EXIT_CODE)

        if activation_path.exists():
            provider_activation_link = read_plaintext_config(activation_path)
        else:
            provider_activation_link = NOCONNECT_LINK

        result.append(
            Provider(name=provider_name, config=provider_config, activation_link=provider_activation_link)
        )

    logging.debug("Configs loaded %s", result)

    return result
