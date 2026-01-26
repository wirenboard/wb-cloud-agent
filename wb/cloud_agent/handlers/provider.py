import logging

from wb.cloud_agent.constants import APP_DATA_PROVIDERS_DIR, PROVIDERS_CONF_DIR
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.settings import AppSettings, delete_provider_config
from wb.cloud_agent.utils import stop_and_disable_service


def delete_provider(settings: AppSettings, _: dict, __: MQTTCloudAgent) -> None:
    """
    Stops and disables all services related to the provider,
    deletes configuration files.
    """
    logging.debug("Deleting provider: %s", settings.provider_name)

    stop_and_disable_service(f"wb-cloud-agent-frpc@{settings.provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-telegraf@{settings.provider_name}.service")

    delete_provider_config(PROVIDERS_CONF_DIR, settings.provider_name)
    delete_provider_config(APP_DATA_PROVIDERS_DIR, settings.provider_name)

    logging.info("Provider %s successfully deleted", settings.provider_name)

    stop_and_disable_service(f"wb-cloud-agent@{settings.provider_name}.service")
