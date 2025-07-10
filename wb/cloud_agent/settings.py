import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse, urlunparse

from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.cloud_agent.constants import (
    APP_DATA_DIR,
    APP_DATA_PROVIDERS_DIR,
    CLOUD_AGENT_URL_POSTFIX,
    DEFAULT_PROVIDER_CONF_FILE,
    NOCONNECT_LINK,
    PROVIDERS_CONF_DIR,
)
from wb.cloud_agent.utils import (
    get_controller_url,
    read_json_config,
    read_plaintext_config,
)


class AppSettings:  # pylint: disable=too-many-instance-attributes disable=too-few-public-methods
    """
    Simple settings configurator.

    To rewrite parameters just add them to wb-cloud-agent config.

    An example of config at /etc/wb-cloud-agent.conf:

    {
        "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:04:C0:00",
    }
    """

    log_level: str = "INFO"

    broker_url: str = DEFAULT_BROKER_URL

    client_cert_engine_key: str = "ATECCx08:00:02:C0:00"
    client_cert_file: str = f"{APP_DATA_DIR}/device_bundle.crt.pem"

    cloud_base_url: str = "https://wirenboard.cloud"
    cloud_agent_url: str = f"https://agent.wirenboard.cloud{CLOUD_AGENT_URL_POSTFIX}"
    request_period_seconds: int = 10

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.config_file: Path = Path(f"{PROVIDERS_CONF_DIR}/{provider}/wb-cloud-agent.conf")
        self.frp_service: str = f"wb-cloud-agent-frpc@{provider}.service"
        self.telegraf_service: str = f"wb-cloud-agent-telegraf@{provider}.service"
        self.frp_config: Path = Path(f"{APP_DATA_PROVIDERS_DIR}/{provider}/frpc.conf")
        self.telegraf_config: Path = Path(f"{APP_DATA_PROVIDERS_DIR}/{provider}/telegraf.conf")
        self.activation_link_config: Path = Path(f"{APP_DATA_PROVIDERS_DIR}/{provider}/activation_link.conf")
        self.mqtt_prefix: str = f"/devices/system__wb-cloud-agent__{provider}"
        self.diag_archive: Path = Path("/tmp")

        if self.config_file.exists():
            self.apply_conf_file()

    def apply_conf_file(self) -> None:
        conf = read_json_config(self.config_file)

        self.cloud_agent_url = self.base_url_to_agent_url(conf["CLOUD_BASE_URL"])

        for key, val in conf.items():
            setattr(self, key.lower(), val)

    def base_url_to_agent_url(self, base_url: str) -> str:
        parsed = urlparse(base_url)
        netloc = f"agent.{parsed.netloc}"
        return urlunparse((parsed.scheme, netloc, CLOUD_AGENT_URL_POSTFIX, "", "", ""))


def configure_app(provider_name: str) -> AppSettings:
    try:
        settings = AppSettings(provider_name)
    except (FileNotFoundError, OSError, json.decoder.JSONDecodeError):
        return 6  # systemd status=6/NOTCONFIGURED

    setup_log(settings)
    return settings


def setup_log(settings: AppSettings) -> None:
    numeric_level = getattr(logging, settings.log_level.upper(), logging.NOTSET)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {settings.log_level}")
    logging.basicConfig(level=numeric_level, encoding="utf-8", format="%(message)s")


def generate_provider_config(provider: str, base_url: str) -> None:
    conf = read_json_config(Path(DEFAULT_PROVIDER_CONF_FILE))
    conf["CLOUD_BASE_URL"] = base_url

    conf_dir = Path(PROVIDERS_CONF_DIR) / provider
    if not conf_dir.exists():
        conf_dir.mkdir(parents=True, exist_ok=True)

    conf_file = conf_dir / "wb-cloud-agent.conf"
    conf_file.write_text(json.dumps(conf, indent=4), encoding="utf-8")


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

        if config_path.exists():
            provider_config = read_json_config(config_path)
        else:
            print(f"The file was not found in: {config_path}")
            sys.exit(6)

        if activation_path.exists():
            provider_activation_link = read_plaintext_config(activation_path)
        else:
            provider_activation_link = NOCONNECT_LINK

        result.append(
            Provider(name=provider_name, config=provider_config, activation_link=provider_activation_link)
        )

    logging.debug("Configs loaded %s", result)

    return result
