import json
import logging
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from wb_common.mqtt_client import DEFAULT_BROKER_URL

DEFAULT_PROVIDER_CONF_FILE = "/etc/wb-cloud-agent.conf"
PROVIDERS_CONF_DIR = "/etc/wb-cloud-agent/providers"
APP_DATA_DIR = "/var/lib/wb-cloud-agent"
APP_DATA_PROVIDERS_DIR = f"{APP_DATA_DIR}/providers"
CLOUD_AGENT_URL_POSTFIX = "/api-agent/v1/"


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
        self.frp_config: str = f"{APP_DATA_PROVIDERS_DIR}/{provider}/frpc.conf"
        self.telegraf_config: str = f"{APP_DATA_PROVIDERS_DIR}/{provider}/telegraf.conf"
        self.activation_link_config: Path = Path(f"{APP_DATA_PROVIDERS_DIR}/{provider}/activation_link.conf")
        self.mqtt_prefix: str = f"/devices/system__wb-cloud-agent__{provider}"
        self.diag_archive: Path = Path("/tmp")

        if self.config_file.exists():
            self.apply_conf_file()

    def apply_conf_file(self) -> None:
        conf = read_json_config(self.config_file)

        self.cloud_agent_url = base_url_to_agent_url(conf["CLOUD_BASE_URL"])

        for key, val in conf.items():
            setattr(self, key.lower(), val)


def base_url_to_agent_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    netloc = f"agent.{parsed.netloc}"
    return urlunparse((parsed.scheme, netloc, CLOUD_AGENT_URL_POSTFIX, "", "", ""))


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


def get_providers() -> list[str]:
    conf_path = Path(PROVIDERS_CONF_DIR)

    if conf_path.exists():
        return [d.name for d in conf_path.iterdir() if d.is_dir()]
    return []


def read_json_config(config_path: Path) -> dict[str, str]:
    data = config_path.read_text(encoding="utf-8")
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        print(f"Error parsing JSON in: {config_path}")
        sys.exit(6)


def read_plaintext_config(config_path: Path) -> str:
    with config_path.open("r", encoding="utf-8") as f:
        return f.readline().strip()


def load_configs(reader: Callable, providers: list[str], providers_path: str) -> dict[str, Any]:
    logging.debug("Load configs providers = %s providers_path = %s", providers, providers_path)
    configs = {}

    for provider in providers:
        config_path = Path(providers_path.format(provider=provider))

        if config_path.exists():
            provider_config = reader(config_path)
        else:
            print(f"The file was not found in: {config_path}")
            sys.exit(6)

        configs[provider] = provider_config

    return configs


def load_providers_configs(providers: list[str]) -> dict[str, dict[str, str]]:
    providers_configs = load_configs(
        read_json_config, providers, f"{PROVIDERS_CONF_DIR}/{{provider}}/wb-cloud-agent.conf"
    )
    logging.debug("providers = %s providers_configs = %s", providers, providers_configs)
    return providers_configs


def load_providers_activation_links(providers: list[str]) -> dict[str, dict[str, str]]:
    providers_activation_links = load_configs(
        read_plaintext_config, providers, f"{APP_DATA_PROVIDERS_DIR}/{{provider}}/activation_link.conf"
    )
    logging.debug("providers = %s providers_activation_links = %s", providers, providers_activation_links)
    return providers_activation_links
