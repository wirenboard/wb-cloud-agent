import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from wb_common.mqtt_client import DEFAULT_BROKER_URL

DEFAULT_CONF_FILE = "/etc/wb-cloud-agent.conf"
PROVIDERS_CONF_DIR = "/etc/wb-cloud-agent/providers"


class AppSettings:  # pylint: disable=too-many-instance-attributes disable=too-few-public-methods
    """
    Simple settings configurator.

    To rewrite parameters just add them to wb-cloud-agent config.

    An example of config at /etc/wb-cloud-agent.conf:

    {
        "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:04:C0:00",
    }
    """

    LOG_LEVEL: str = "INFO"
    BROKER_URL: str = DEFAULT_BROKER_URL

    CLIENT_CERT_ENGINE_KEY: str = "ATECCx08:00:02:C0:00"
    CLIENT_CERT_FILE: str = "/var/lib/wb-cloud-agent/device_bundle.crt.pem"
    CLOUD_BASE_URL: str = "https://wirenboard.cloud"
    CLOUD_AGENT_URL: str = "https://agent.wirenboard.cloud/api-agent/v1/"
    REQUEST_PERIOD_SECONDS: int = 3

    def __init__(self, provider: str = "default"):
        self.PROVIDER = provider  # pylint:disable=invalid-name
        if provider == "default":
            self.CONFIG_FILE: str = DEFAULT_CONF_FILE  # pylint:disable=invalid-name
            self.FRP_SERVICE: str = "wb-cloud-agent-frpc.service"  # pylint:disable=invalid-name
            self.TELEGRAF_SERVICE: str = "wb-cloud-agent-telegraf.service"  # pylint:disable=invalid-name
            self.FRP_CONFIG: str = "/var/lib/wb-cloud-agent/frpc.conf"  # pylint:disable=invalid-name
            self.TELEGRAF_CONFIG: str = "/var/lib/wb-cloud-agent/telegraf.conf"  # pylint:disable=invalid-name
            self.ACTIVATION_LINK_CONFIG: str = (  # pylint:disable=invalid-name
                "/var/lib/wb-cloud-agent/activation_link.conf"
            )
        else:
            self.CONFIG_FILE: str = f"{PROVIDERS_CONF_DIR}/{provider}/wb-cloud-agent.conf"
            self.FRP_SERVICE: str = f"wb-cloud-agent-frpc@{provider}.service"
            self.TELEGRAF_SERVICE: str = f"wb-cloud-agent-telegraf@{provider}.service"
            self.FRP_CONFIG: str = f"/var/lib/wb-cloud-agent/providers/{provider}/frpc.conf"
            self.TELEGRAF_CONFIG: str = f"/var/lib/wb-cloud-agent/providers/{provider}/telegraf.conf"
            self.ACTIVATION_LINK_CONFIG: str = (
                f"/var/lib/wb-cloud-agent/providers/{provider}/activation_link.conf"
            )
        self.MQTT_PREFIX: str = f"/devices/system__wb-cloud-agent__{provider}"  # pylint:disable=invalid-name

        if os.path.exists(self.CONFIG_FILE):
            self.apply_conf_file(self.CONFIG_FILE)

    def apply_conf_file(self, conf_file: str) -> None:
        conf = json.loads(Path(conf_file).read_text(encoding="utf-8"))
        for key in conf:
            setattr(self, key, conf[key])


def base_url_to_agent_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    netloc = f"agent.{parsed.netloc}"
    return urlunparse((parsed.scheme, netloc, "/api-agent/v1/", '', '', ''))


def generate_config(provider: str, base_url: str) -> None:
    if provider == "default":
        return

    conf = json.loads(Path(DEFAULT_CONF_FILE).read_text(encoding="utf-8"))
    conf["CLOUD_BASE_URL"] = base_url
    conf["CLOUD_AGENT_URL"] = base_url_to_agent_url(base_url)

    conf_dir = os.path.join(PROVIDERS_CONF_DIR, provider)
    if not os.path.exists(conf_dir):
        os.makedirs(conf_dir)

    conf_file = Path(f"{PROVIDERS_CONF_DIR}/{provider}/wb-cloud-agent.conf")
    conf_file.write_text(json.dumps(conf, indent=4), encoding="utf-8")


def get_providers() -> list[str]:
    providers = ["default"]
    if os.path.exists(PROVIDERS_CONF_DIR):
        providers += [
            d for d in os.listdir(PROVIDERS_CONF_DIR) if os.path.isdir(os.path.join(PROVIDERS_CONF_DIR, d))
        ]
    return providers


def read_providers_configs(config_path: Path, provider: str, configs: dict) -> None:
    with config_path.open("r", encoding="utf-8") as f:
        try:
            configs[provider] = json.load(f)
        except json.JSONDecodeError:
            print(f"Error parsing JSON in: {config_path}")
            sys.exit(6)


def load_providers_configs(providers: list[str]) -> dict[str, dict[str, str]]:
    configs = {}

    for provider in providers:
        config_path = Path(PROVIDERS_CONF_DIR) / provider / "wb-cloud-agent.conf"
        if config_path.exists():
            read_providers_configs(config_path, provider, configs)
        else:
            config_path = Path(DEFAULT_CONF_FILE)
            if config_path.exists():
                read_providers_configs(config_path, provider, configs)
            else:
                print(f"The file was not found in: {config_path}")
                sys.exit(6)

    return configs
