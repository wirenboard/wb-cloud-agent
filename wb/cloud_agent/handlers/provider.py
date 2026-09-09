import logging

from wb.cloud_agent.constants import (
    APP_DATA_PROVIDERS_DIR,
    PROVIDERS_CONF_DIR,
    UNKNOWN_LINK,
)
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.services.metrics import stop_metrics_health_monitor
from wb.cloud_agent.settings import AppSettings, delete_provider_config
from wb.cloud_agent.utils import stop_and_disable_service


def delete_provider(settings: AppSettings, _: dict, __: MQTTCloudAgent) -> None:
    """
    Stops and disables all services related to the provider,
    deletes configuration files.
    """
    logging.debug("Deleting provider: %s", settings.provider_name)

    stop_and_disable_service(f"wb-cloud-agent-frpc@{settings.provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-metrics@{settings.provider_name}.service")

    delete_provider_config(PROVIDERS_CONF_DIR, settings.provider_name)
    delete_provider_config(APP_DATA_PROVIDERS_DIR, settings.provider_name)

    logging.info("Provider %s successfully deleted", settings.provider_name)

    stop_and_disable_service(f"wb-cloud-agent@{settings.provider_name}.service")


def unbind_provider(settings: AppSettings, _: dict, mqtt: MQTTCloudAgent) -> None:
    """Stop cloud access and clear provider runtime state while keeping its identity."""
    logging.debug("Unbinding provider: %s", settings.provider_name)

    stop_metrics_health_monitor(settings.provider_name)
    stop_and_disable_service(settings.frp_service)
    stop_and_disable_service(settings.metrics_service)

    runtime_dir = settings.activation_link_config.parent
    for runtime_file in (
        settings.frp_config,
        settings.metrics_script,
        settings.metrics_vars_config,
        settings.metrics_last_uid,
        settings.activation_link_config,
        runtime_dir / "connection.token",
    ):
        runtime_file.unlink(missing_ok=True)

    write_activation_link(settings, UNKNOWN_LINK, mqtt)
    logging.info("Provider %s successfully unbound", settings.provider_name)
