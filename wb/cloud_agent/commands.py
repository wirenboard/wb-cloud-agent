import logging
import signal
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from wb.cloud_agent import __version__ as agent_package_version
from wb.cloud_agent.handlers.curl import CloudNetworkError
from wb.cloud_agent.handlers.events import event_delete_controller, make_event_request
from wb.cloud_agent.handlers.ping import wait_for_cloud_reachable
from wb.cloud_agent.handlers.startup import (
    make_start_up_request,
    on_message,
    send_packages_version,
)
from wb.cloud_agent.mqtt import EXIT_STOPPED, MQTTCloudAgent
from wb.cloud_agent.services.activation import read_activation_link
from wb.cloud_agent.services.lifecycle import stop_services_and_del_configs
from wb.cloud_agent.services.metrics import reconcile_metrics_script
from wb.cloud_agent.settings import (
    configure_app,
    generate_provider_config,
    get_provider_names,
    load_providers_data,
)
from wb.cloud_agent.utils import (
    handle_connection_state,
    normalize_base_url,
    show_providers_table,
    start_and_enable_service,
)


def show_providers(_options) -> int:
    provider_names = get_provider_names()
    providers = load_providers_data(provider_names)
    show_providers_table(providers)
    return 0


def add_provider(options) -> int:
    base_url = normalize_base_url(options.base_url)
    provider_name = options.name or urlparse(base_url).netloc
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

    existing_providers = load_providers_data(providers)
    if any(
        normalize_base_url(provider.config["CLOUD_BASE_URL"]) == base_url for provider in existing_providers
    ):
        print(f"Provider with URL {base_url} already exists")
        return 1

    generate_provider_config(provider_name, base_url)
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


def _retry_cloud_startup(settings, mqtt, stop_requested: threading.Event) -> Optional[int]:
    startup_error_reported = False
    while not stop_requested.is_set() and mqtt.fatal_exit_code is None:
        try:
            make_start_up_request(settings, mqtt)
            send_packages_version(settings)
            return None
        except (CloudNetworkError, ValueError, subprocess.TimeoutExpired) as exc:
            if not startup_error_reported:
                logging.info("Cloud startup request failed; retrying")
                startup_error_reported = True
            logging.debug("Cloud startup failure details", exc_info=exc)
            mqtt.publish_ctrl("status", "Network or Cloud is unreachable! Retrying...")
            stop_requested.wait(settings.request_period_seconds)

    if stop_requested.is_set():
        return EXIT_STOPPED
    return mqtt.fatal_exit_code


def _initialize_cloud_agent(settings, mqtt, stop_requested: threading.Event) -> Optional[int]:
    mqtt.update_providers_list()
    mqtt.publish_vdev()
    mqtt.publish_ctrl("activation_link", read_activation_link(settings))
    mqtt.publish_ctrl("cloud_base_url", settings.cloud_base_url)
    mqtt.publish_ctrl("status", "connecting")

    if not wait_for_cloud_reachable(
        settings.cloud_base_url,
        settings.ping_period_seconds,
        max_retries=None,
        stop_requested=stop_requested,
    ):
        return EXIT_STOPPED

    startup_exit_code = _retry_cloud_startup(settings, mqtt, stop_requested)
    if startup_exit_code is not None:
        return startup_exit_code

    reconcile_metrics_script(settings)
    logging.info("Cloud Agent initialization - OK")
    return None


def _make_cloud_event_request(settings, mqtt):
    try:
        make_event_request(settings, mqtt)
        return True, "Cloud Agent is successfully connected to the cloud!", None
    except subprocess.TimeoutExpired as exc:
        return False, "Request timeout. Retrying...", exc
    except CloudNetworkError as exc:
        return False, "Network or Cloud is unreachable! Retrying...", exc
    except Exception:  # pylint:disable=broad-exception-caught
        logging.exception("Cloud connection exception")
        return False, "Error making request to cloud! Retrying...", None


def _run_cloud_event_loop(settings, mqtt, stop_requested: threading.Event) -> int:
    was_connected = False
    while not stop_requested.is_set() and mqtt.fatal_exit_code is None:
        start = time.perf_counter()
        logging.debug("Sending event request")

        conn_state, msg, exc_info = _make_cloud_event_request(settings, mqtt)
        was_connected = handle_connection_state(was_connected, conn_state, msg, mqtt)
        if exc_info is not None:
            logging.debug(msg, exc_info=exc_info)

        logging.debug("Event request completed in %s ms", int((time.perf_counter() - start) * 1000))
        stop_requested.wait(settings.request_period_seconds)

    if mqtt.fatal_exit_code is not None:
        return mqtt.fatal_exit_code

    logging.info("Cloud Agent stopping on request")
    return EXIT_STOPPED


def _run_connected_cloud_agent(settings, mqtt, stop_requested: threading.Event) -> int:
    initialization_exit_code = _initialize_cloud_agent(settings, mqtt, stop_requested)
    if initialization_exit_code is not None:
        return initialization_exit_code
    return _run_cloud_event_loop(settings, mqtt, stop_requested)


def run_daemon(options) -> int:
    settings = configure_app(
        provider_name=options.provider_name,
        config_file=getattr(options, "config", None),
        require_conf_file=True,
    )
    if isinstance(settings, int):
        return settings

    settings.broker_url = options.broker or settings.broker_url
    logging.info(
        "====== Cloud Agent started (version: %s, provider: %s) ======",
        agent_package_version,
        settings.cloud_base_url,
    )

    stop_requested = threading.Event()

    def request_stop(_signum, _frame):
        stop_requested.set()

    mqtt = MQTTCloudAgent(settings, on_message)
    old_handlers = {signum: signal.signal(signum, request_stop) for signum in (signal.SIGTERM, signal.SIGINT)}

    exit_code = None
    try:
        mqtt.start(update_status=True)
        exit_code = mqtt.wait_for_connection(stop_requested)
        if exit_code is None:
            exit_code = _run_connected_cloud_agent(settings, mqtt, stop_requested)
        return exit_code
    finally:
        if exit_code == EXIT_STOPPED:
            mqtt.remove_vdev()
        mqtt.stop()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
