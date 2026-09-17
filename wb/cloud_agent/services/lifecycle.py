import logging
import threading

from wb.cloud_agent.constants import (
    APP_DATA_PROVIDERS_DIR,
    PROVIDERS_CONF_DIR,
    UNBIND_CTRL_REQUEST_TIMEOUT,
    UNKNOWN_LINK,
)
from wb.cloud_agent.handlers.events import event_delete_controller
from wb.cloud_agent.services.activation import read_activation_link
from wb.cloud_agent.settings import AppSettings, delete_provider_config
from wb.cloud_agent.utils import stop_and_disable_service


def stop_services_and_del_configs(settings: AppSettings, provider_name: str, unbind: bool = True) -> None:
    logging.debug("Deleting provider: %s", provider_name)

    activation_link = read_activation_link(settings)
    if activation_link == UNKNOWN_LINK and not unbind:
        logging.warning(
            "Provider %s config is unusable, so its cloud address is unknown: the controller stays "
            "bound there, detach it with `wb-cloud-agent cloud-unbind <url>`",
            provider_name,
        )
    unbind = unbind and activation_link == UNKNOWN_LINK

    if unbind:
        thread = threading.Thread(target=event_delete_controller, args=(settings,), daemon=True)
        thread.start()

    stop_and_disable_service(f"wb-cloud-agent@{provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-frpc@{provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-metrics@{provider_name}.service")

    delete_provider_config(PROVIDERS_CONF_DIR, provider_name)
    delete_provider_config(APP_DATA_PROVIDERS_DIR, provider_name)

    if unbind:
        thread.join(timeout=UNBIND_CTRL_REQUEST_TIMEOUT + 1)

    logging.info("Provider %s successfully deleted", provider_name)
