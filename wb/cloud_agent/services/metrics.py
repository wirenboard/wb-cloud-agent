from string import Template

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import start_and_enable_service, write_to_file


def update_metrics_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    write_to_file(
        fpath=settings.telegraf_config,
        contents=Template(payload["config"]).safe_substitute(BROKER_URL=settings.broker_url),
    )
    start_and_enable_service(settings.telegraf_service, restart=True)
    write_activation_link(settings, UNKNOWN_LINK, mqtt)
