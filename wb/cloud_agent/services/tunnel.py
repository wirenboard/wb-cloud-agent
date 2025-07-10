from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import start_and_enable_service, write_to_file


def update_tunnel_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    write_to_file(fpath=settings.frp_config, contents=payload["config"])
    start_and_enable_service(settings.frp_service, restart=True)
    write_activation_link(settings, UNKNOWN_LINK, mqtt)
