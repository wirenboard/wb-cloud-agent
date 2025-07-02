from wb.cloud_agent.constants import APP_DATA_PROVIDERS_DIR, PROVIDERS_CONF_DIR
from wb.cloud_agent.settings import delete_provider_config
from wb.cloud_agent.utils import stop_and_disable_service


def stop_services_and_del_configs(provider_name: str) -> None:
    stop_and_disable_service(f"wb-cloud-agent@{provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-frpc@{provider_name}.service")
    stop_and_disable_service(f"wb-cloud-agent-telegraf@{provider_name}.service")
    delete_provider_config(PROVIDERS_CONF_DIR, provider_name)
    delete_provider_config(APP_DATA_PROVIDERS_DIR, provider_name)
    print(f"Provider {provider_name} successfully deleted")
