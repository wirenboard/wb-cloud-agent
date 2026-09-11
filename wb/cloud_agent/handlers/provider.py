import logging

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.services.metrics import (
    _safe_stop_and_disable_service,
    stop_metrics_health_monitor,
)
from wb.cloud_agent.settings import AppSettings


def unbind_provider(settings: AppSettings, _: dict, mqtt: MQTTCloudAgent) -> None:
    """Stop cloud access and clear provider runtime state while keeping its identity."""
    logging.debug("Unbinding provider: %s", settings.provider_name)

    stop_metrics_health_monitor(settings.provider_name)
    _safe_stop_and_disable_service(settings.frp_service)
    _safe_stop_and_disable_service(settings.metrics_service)

    for runtime_file in settings.runtime_files:
        runtime_file.unlink(missing_ok=True)

    write_activation_link(settings, UNKNOWN_LINK, mqtt)
    logging.info("Provider %s successfully unbound", settings.provider_name)
