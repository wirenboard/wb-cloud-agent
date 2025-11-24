import logging
import subprocess
import time
from contextlib import ExitStack
from urllib.parse import urlparse

from wb.cloud_agent.handlers.events import event_delete_controller, make_event_request
from wb.cloud_agent.handlers.ping import wait_for_ping
from wb.cloud_agent.handlers.startup import (
    make_start_up_request,
    on_message,
    send_agent_version,
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
from wb.cloud_agent.utils import show_providers_table, start_and_enable_service


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


def run_daemon(options) -> int | None:
    settings = configure_app(provider_name=options.provider_name)
    settings.broker_url = options.broker or settings.broker_url

    cloud_host = urlparse(settings.cloud_base_url).hostname
    wait_for_ping(cloud_host, period=settings.request_period_seconds)

    mqtt = MQTTCloudAgent(settings, on_message)
    try:
        mqtt.start(update_status=True)
    except Exception as ex:  # pylint:disable=broad-exception-caught
        logging.error("Error starting MQTT client: %s", ex)

    try:
        make_start_up_request(settings, mqtt)
        send_agent_version(settings)
    except RuntimeError as exc:
        logging.error("Startup request failed: %s", exc)
        return 1

    mqtt.update_providers_list()
    mqtt.publish_vdev()
    mqtt.publish_ctrl("activation_link", read_activation_link(settings))
    mqtt.publish_ctrl("cloud_base_url", settings.cloud_base_url)
    mqtt.publish_ctrl("status", "connecting")

    with ExitStack() as stack:
        stack.callback(mqtt.remove_vdev)

        while True:
            start = time.perf_counter()
            logging.debug("Starting request for events sent")

            try:
                make_event_request(settings, mqtt)
            except subprocess.TimeoutExpired:
                logging.debug("Timeout when executing request for events sent")
                continue
            except Exception:  # pylint:disable=broad-exception-caught
                err_msg = "Error making request to cloud!"
                logging.exception(err_msg)
                mqtt.publish_ctrl("status", err_msg)
            else:
                mqtt.publish_ctrl("status", "ok")

            request_time = time.perf_counter() - start
            logging.debug("Request for events sent done in: %s ms.", int(request_time * 1000))
            time.sleep(settings.request_period_seconds)
