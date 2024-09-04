import json
import os
from json import JSONDecodeError
from pathlib import Path

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
        self.PROVIDER = provider
        if provider == "default":
            self.CONFIG_FILE: str = DEFAULT_CONF_FILE
            self.FRP_SERVICE: str = "wb-cloud-agent-frpc.service"
            self.TELEGRAF_SERVICE: str = "wb-cloud-agent-telegraf.service"
            self.FRP_CONFIG: str = "/run/frpc.conf"
            self.TELEGRAF_CONFIG: str = "/var/lib/wb-cloud-agent/telegraf.conf"
            self.ACTIVATION_LINK_CONFIG: str = "/var/lib/wb-cloud-agent/activation_link.conf"
        else:
            self.CONFIG_FILE: str = f"{PROVIDERS_CONF_DIR}/{provider}/wb-cloud-agent.conf"
            self.FRP_SERVICE: str = f"wb-cloud-agent-frpc@{provider}.service"
            self.TELEGRAF_SERVICE: str = f"wb-cloud-agent-telegraf@{provider}.service"
            self.FRP_CONFIG: str = f"/run/frpc-{provider}.conf"
            self.TELEGRAF_CONFIG: str = f"/var/lib/wb-cloud-agent/providers/{provider}/telegraf.conf"
            self.ACTIVATION_LINK_CONFIG: str = (
                f"/var/lib/wb-cloud-agent/providers/{provider}/activation_link.conf"
            )
        self.MQTT_PREFIX: str = f"/devices/system__wb-cloud-agent__{provider}"

        if os.path.exists(self.CONFIG_FILE):
            self.apply_conf_file(self.CONFIG_FILE)

    def apply_conf_file(self, conf_file: str) -> None:
        conf = json.loads(Path(conf_file).read_text(encoding="utf-8"))
        for key in conf:
            setattr(self, key, conf[key])


def generate_config(provider: str, base_url: str, agent_url: str) -> None:
    if provider == "default":
        return

    conf = json.loads(Path(DEFAULT_CONF_FILE).read_text(encoding="utf-8"))
    conf["CLOUD_BASE_URL"] = base_url
    conf["CLOUD_AGENT_URL"] = agent_url

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
