import logging
import subprocess
import time
from contextlib import ExitStack
from typing import Optional
from urllib.parse import urlparse

from wb.cloud_agent.handlers.curl import CloudNetworkError
from wb.cloud_agent.handlers.events import event_delete_controller, make_event_request
from wb.cloud_agent.handlers.ping import CloudUnreachableError, wait_for_cloud_reachable
from wb.cloud_agent.handlers.startup import (
    make_start_up_request,
    on_message,
    send_packages_version,
)
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import read_activation_link
from wb.cloud_agent.services.lifecycle import stop_services_and_del_configs
from wb.cloud_agent.settings import (
    configure_app,
    generate_provider_config,
    get_provider_names,
    load_providers_data,
)
from wb.cloud_agent.utils import (
    handle_connection_state,
    show_providers_table,
    start_and_enable_service,
)


def show_providers(_options) -> int:
    provider_names = get_provider_names()
    providers = load_providers_data(provider_names)
    show_providers_table(providers)
    return 0


def add_provider(options) -> int:
    provider_name = options.name or urlparse(options.base_url).netloc
    settings = configure_app(provider_name=provider_name)

    try:
        mqtt = MQTTCloudAgent(settings, on_message)
        mqtt.start()
    except (FileNotFoundError, ConnectionError) as exc:
        logging.error("Error starting MQTT client: %s", exc)

    providers = get_provider_names()
    if provider_name in providers:
        print(f"Provider {provider_name} already exists")
        return 1

    generate_provider_config(provider_name, options.base_url)
    start_and_enable_service(f"wb-cloud-agent@{provider_name}.service")

    try:
        mqtt.update_providers_list()
    except (FileNotFoundError, ConnectionError) as exc:
        logging.error("Error publish MQTT providers: %s", exc)

    print(f"Provider {provider_name} successfully added")
    return 0


def add_on_premise_provider(options) -> int:
    del_all_providers(options, show_msg=False)
    return add_provider(options)


def del_provider(options) -> int:
    provider_name = urlparse(options.provider_name).netloc or options.provider_name
    settings = configure_app(provider_name=provider_name)

    mqtt = MQTTCloudAgent(settings, on_message)
    mqtt.start()

    providers = get_provider_names()
    if provider_name not in providers:
        print(f"Provider {provider_name} does not exists")
        return 1

    stop_services_and_del_configs(settings, provider_name)
    mqtt.update_providers_list()
    return 0


def del_all_providers(_options, show_msg: bool = True) -> int:
    providers = get_provider_names()
    if not providers:
        if show_msg:
            print("No one provider was found")
        return 1

    for provider_name in providers:
        settings = configure_app(provider_name=provider_name)

        mqtt = MQTTCloudAgent(settings, on_message)
        mqtt.start()

        stop_services_and_del_configs(settings, provider_name)
        mqtt.update_providers_list()
    return 0


def del_controller_from_cloud(options) -> int:
    settings = configure_app(provider_name="", skip_conf_file=True, cloud_base_url=options.base_url)
    return event_delete_controller(settings)


def run_daemon(options) -> Optional[int]:
    settings = configure_app(provider_name=options.provider_name)
    settings.broker_url = options.broker or settings.broker_url
    logging.info("====== Cloud Agent started (provider: %s) ======", settings.cloud_base_url)

    try:
        wait_for_cloud_reachable(settings.cloud_base_url, settings.ping_period_seconds)
    except CloudUnreachableError as exc:
        logging.error(str(exc))
        logging.debug("Cloud reachability failure details", exc_info=exc)
        return 1

    mqtt = MQTTCloudAgent(settings, on_message)
    try:
        mqtt.start(update_status=True)
    except Exception as exc:  # pylint:disable=broad-exception-caught
        logging.error("Error starting MQTT client: %s", exc)

    try:
        make_start_up_request(settings, mqtt)
        send_packages_version(settings)
    except CloudNetworkError as exc:
        logging.error("Startup request failed: %s", exc)
        return 1

    mqtt.update_providers_list()
    mqtt.publish_vdev()
    mqtt.publish_ctrl("activation_link", read_activation_link(settings))
    mqtt.publish_ctrl("cloud_base_url", settings.cloud_base_url)
    mqtt.publish_ctrl("status", "connecting")

    logging.info("Cloud Agent initialization - OK")

    with ExitStack() as stack:
        stack.callback(mqtt.remove_vdev)
        was_connected = False

        while True:
            start = time.perf_counter()
            logging.debug("Sending event request")

            try:
                make_event_request(settings, mqtt)
                conn_state, msg, exc_info = True, "Cloud Agent is successfully connected to the cloud!", None

            except subprocess.TimeoutExpired as exc:
                conn_state, msg, exc_info = False, "Request timeout. Retrying...", exc

            except CloudNetworkError as exc:
                conn_state, msg, exc_info = False, "Network or Cloud is unreachable! Retrying...", exc

            except Exception:  # pylint:disable=broad-exception-caught
                logging.exception("Cloud connection exception")
                conn_state, msg, exc_info = False, "Error making request to cloud! Retrying...", None

            was_connected = handle_connection_state(was_connected, conn_state, msg, mqtt)

            if exc_info is not None:
                logging.debug(msg, exc_info=exc_info)

            logging.debug("Event request completed in %s ms", int((time.perf_counter() - start) * 1000))
            time.sleep(settings.request_period_seconds)
