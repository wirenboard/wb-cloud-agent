import threading

from wb.cloud_agent.constants import (
    APP_DATA_PROVIDERS_DIR,
    PROVIDERS_CONF_DIR,
    UNKNOWN_LINK,
)
from wb.cloud_agent.handlers.events import event_delete_controller
from wb.cloud_agent.services.activation import read_activation_link
from wb.cloud_agent.settings import AppSettings, delete_provider_config
from wb.cloud_agent.utils import stop_and_disable_service


def stop_services_and_del_configs(settings: AppSettings, provider_name: str) -> None:
    activation_link = read_activation_link(settings)

    if activation_link == UNKNOWN_LINK:
        thread = threading.Thread(target=event_delete_controller, args=(settings,), daemon=True)
        thread.start()

    stop_and_disable_service(f"wb-cloud-agent@{provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-frpc@{provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-telegraf@{provider_name}.service")

    delete_provider_config(PROVIDERS_CONF_DIR, provider_name)
    delete_provider_config(APP_DATA_PROVIDERS_DIR, provider_name)

    if activation_link == UNKNOWN_LINK:
        thread.join(timeout=8)

    print(f"Provider {provider_name} successfully deleted")
