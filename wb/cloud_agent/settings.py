import json
import logging
import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
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
    PROVIDER_CONF_FILE_NAME,
    PROVIDERS_CONF_DIR,
)
from wb.cloud_agent.utils import (
    ConfigError,
    config_recovery_lock,
    get_controller_url,
    normalize_base_url,
    quarantine_broken_file,
    read_json_config,
    read_plaintext_config,
    write_to_file,
)

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_CLOUD_BASE_URL = "https://wirenboard.cloud"


def provider_config_path(provider_name: str) -> Path:
    return Path(PROVIDERS_CONF_DIR) / provider_name / PROVIDER_CONF_FILE_NAME


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

    log_level: str = DEFAULT_LOG_LEVEL

    broker_url: str = DEFAULT_BROKER_URL

    client_cert_engine_key: str = "ATECCx08:00:02:C0:00"
    client_cert_file: str = f"{APP_DATA_DIR}/device_bundle.crt.pem"

    cloud_base_url: str = DEFAULT_CLOUD_BASE_URL
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
        self.config_error: Optional[str] = None

        self.reload_config()

    def reload_config(self) -> None:
        """Re-apply the provider config file, rebuilding a damaged file when requested."""
        self.config_error = None
        # Outside the daemon a missing config is not an anomaly: add-provider is about to create it.
        if not self.skip_conf_file and (self.recover_configs or self.config_file.exists()):
            self.apply_conf_file()

        self.cloud_base_url = normalize_base_url(self.cloud_base_url)
        self.cloud_agent_url = self.base_url_to_agent_url(self.cloud_base_url)

    def apply_conf_file(self) -> None:
        try:
            conf = read_json_config(
                self.config_file,
                rebuild=partial(recover_provider_config, self.provider_name, self.recover_configs),
            )
        except ConfigError as exc:
            logging.warning("Config %s %s", self.config_file, exc)
            self.config_error = str(exc)
            return

        for key, val in conf.items():
            setattr(self, key.lower(), val)

    def base_url_to_agent_url(self, base_url: str) -> str:
        parsed = urlparse(normalize_base_url(base_url))
        netloc = f"agent.{parsed.netloc}"
        return urlunparse((parsed.scheme, netloc, CLOUD_AGENT_URL_POSTFIX, "", "", ""))


def configure_app(**kwargs: dict[str, Any]) -> AppSettings:
    # Configured first so a recovery WARNING from AppSettings still comes out in the configured format.
    setup_log(DEFAULT_LOG_LEVEL)
    settings = AppSettings(**kwargs)
    setup_log(settings.log_level)
    return settings


def setup_log(log_level: str) -> None:
    numeric_level = getattr(logging, log_level.upper(), logging.NOTSET)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")
    logging.basicConfig(level=numeric_level, encoding="utf-8", format="%(message)s", force=True)


def generate_provider_config(provider: str, base_url: str) -> None:
    conf = _packaged_default_config()
    conf["CLOUD_BASE_URL"] = normalize_base_url(base_url)
    write_to_file(provider_config_path(provider), json.dumps(conf, indent=4))


def _packaged_default_config() -> dict[str, str]:
    """Read the packaged defaults, falling back to values safe for production."""
    defaults = {
        "LOG_LEVEL": DEFAULT_LOG_LEVEL,
        "CLIENT_CERT_ENGINE_KEY": AppSettings.client_cert_engine_key,
        "CLOUD_BASE_URL": DEFAULT_CLOUD_BASE_URL,
    }
    try:
        defaults.update(read_json_config(Path(DEFAULT_PROVIDER_CONF_FILE)))
        return defaults
    except ConfigError as exc:
        logging.warning("Config %s %s, using built-in values", DEFAULT_PROVIDER_CONF_FILE, exc)
        return defaults


def recover_provider_config(provider_name: str, persist: bool, reason: str) -> dict:
    """Use the production defaults for a damaged provider config."""
    config_path = provider_config_path(provider_name)
    recovered = _packaged_default_config()
    recovered["CLOUD_BASE_URL"] = DEFAULT_CLOUD_BASE_URL

    try:
        recovery_lock = config_recovery_lock(config_path) if persist else nullcontext()
        with recovery_lock:
            # Another agent process may have repaired the file after the initial parse failed.
            try:
                return read_json_config(config_path)
            except ConfigError:
                pass

            if not persist:
                logging.warning("Config %s %s, using production defaults for this run", config_path, reason)
                return recovered

            try:
                broken_size = config_path.stat().st_size
            except FileNotFoundError:
                broken_size = 0
            except OSError:
                broken_size = None

            quarantined = quarantine_broken_file(config_path) if broken_size else None
            if broken_size is None or (broken_size > 0 and quarantined is None):
                logging.warning(
                    "Config %s %s, cannot preserve the broken file; using production defaults for this run",
                    config_path,
                    reason,
                )
                return recovered

            try:
                write_to_file(config_path, json.dumps(recovered, indent=4))
            except OSError as exc:
                logging.warning(
                    "Config %s %s, cannot be rewritten (%s); using production defaults for this run",
                    config_path,
                    reason,
                    exc,
                )
                return recovered

            logging.warning(
                "Config %s %s, restored to production defaults%s",
                config_path,
                reason,
                f", broken file kept as {quarantined.name}" if quarantined else "",
            )
            return recovered
    except OSError as exc:
        logging.warning(
            "Config %s %s, recovery is not persistent (%s); using production defaults for this run",
            config_path,
            reason,
            exc,
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
        base_url = self.config.get("CLOUD_BASE_URL")
        if not base_url:
            return "Broken configuration"

        if self.activation_link and self.activation_link.startswith("http"):
            return self.activation_link

        if self.activation_link == NOCONNECT_LINK:
            return f"No connect to: {base_url}"

        return get_controller_url(base_url)


def load_providers_data(provider_names: list[str]) -> list[Provider]:
    """Load provider configs and actiovation links from files"""
    logging.debug("Load configs for provider_names = %s", provider_names)

    result = []
    for provider_name in provider_names:
        try:
            provider_config = read_json_config(
                provider_config_path(provider_name),
                rebuild=partial(recover_provider_config, provider_name, False),
            )
        except ConfigError as exc:
            logging.warning("Provider %s config %s", provider_name, exc)
            provider_config = {}

        activation_path = Path(f"{APP_DATA_PROVIDERS_DIR}/{provider_name}/activation_link.conf")
        provider_activation_link = read_plaintext_config(activation_path) or NOCONNECT_LINK

        result.append(
            Provider(name=provider_name, config=provider_config, activation_link=provider_activation_link)
        )

    logging.debug("Configs loaded %s", result)

    return result
