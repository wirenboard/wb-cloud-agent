import logging

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import read_plaintext_config, write_to_file


def read_activation_link(settings: AppSettings) -> str:
    logging.debug("Read activation link from %s", settings.activation_link_config)

    activation_link = read_plaintext_config(settings.activation_link_config) or UNKNOWN_LINK

    logging.debug("Activation link read %s", activation_link)
    return activation_link


def update_activation_link(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    write_activation_link(settings, payload["activationLink"], mqtt)


def write_activation_link(settings: AppSettings, link: str, mqtt) -> None:
    logging.debug("Write activation link %s to %s", link, settings.activation_link_config)
    write_to_file(fpath=settings.activation_link_config, contents=link)
    mqtt.publish_ctrl("activation_link", link)
