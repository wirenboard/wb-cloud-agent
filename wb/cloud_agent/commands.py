import logging
import signal
import subprocess
import threading
import time
from urllib.parse import urlparse

from wb.cloud_agent import __version__ as agent_package_version
from wb.cloud_agent.constants import (
    EXIT_INVALIDARGUMENT,
    EXIT_NOTCONFIGURED,
    EXIT_SUCCESS,
)
from wb.cloud_agent.handlers.curl import CloudNetworkError
from wb.cloud_agent.handlers.events import event_delete_controller, make_event_request
from wb.cloud_agent.handlers.ping import wait_for_cloud_reachable
from wb.cloud_agent.handlers.startup import (
    make_start_up_request,
    on_message,
    send_packages_version,
)
from wb.cloud_agent.mqtt import MQTTCloudAgent, check_broker_url
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


def _register_in_cloud(settings, mqtt: MQTTCloudAgent, stop_requested: threading.Event) -> bool:
    """
    Repeat the startup requests until the cloud accepts them; False if a stop was requested first.
    """
    while wait_for_cloud_reachable(settings.cloud_base_url, settings.ping_period_seconds, stop_requested):
        try:
            make_start_up_request(settings, mqtt)
            send_packages_version(settings)
            return True
        except Exception as exc:  # pylint:disable=broad-exception-caught
            # Retried like the event loop does: the cloud answers, but the request did not go through.
            logging.error("Startup request failed: %s. Retrying...", exc)
            mqtt.publish_ctrl("status", "Network or Cloud is unreachable! Retrying...")
            stop_requested.wait(settings.request_period_seconds)
    return False


def _serve_cloud(settings, mqtt: MQTTCloudAgent, stop_requested: threading.Event) -> None:
    """
    Publish the virtual device, register in the cloud and poll its events until a stop is requested.
    """
    mqtt.publish_ctrl("status", "starting")
    mqtt.update_providers_list()
    mqtt.publish_vdev()
    mqtt.publish_ctrl("activation_link", read_activation_link(settings))
    mqtt.publish_ctrl("cloud_base_url", settings.cloud_base_url)

    if not _register_in_cloud(settings, mqtt, stop_requested):
        return

    mqtt.publish_ctrl("status", "connecting")
    reconcile_metrics_script(settings)
    logging.info("Cloud Agent initialization - OK")

    was_connected = False
    while not stop_requested.is_set():
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
        stop_requested.wait(settings.request_period_seconds)


def run_daemon(options) -> int:
    settings = configure_app(
        provider_name=options.provider_name, config_file=options.config, require_conf_file=True
    )
    settings.broker_url = options.broker or settings.broker_url
    try:
        # --broker is checked by argparse, so an error here comes from the config file
        check_broker_url(settings.broker_url)
    except ValueError as exc:
        logging.error("%s", exc)
        return EXIT_NOTCONFIGURED
    logging.info(
        "====== Cloud Agent started (version: %s, provider: %s) ======",
        agent_package_version,
        settings.cloud_base_url,
    )

    stop_requested = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop_requested.set())

    mqtt = MQTTCloudAgent(settings, on_message, stop_requested)
    mqtt.start(daemon=True)
    if mqtt.wait_for_connection():
        _serve_cloud(settings, mqtt, stop_requested)
    if mqtt.authentication_failed:
        mqtt.stop()
        return EXIT_INVALIDARGUMENT

    # Only the agent itself can clear its retained topics, so this runs on a requested stop only.
    # After a crash the topics stay in place and the Last Will marks the status as stopped.
    mqtt.remove_vdev()
    mqtt.stop()
    logging.info("Cloud Agent stopped")
    return EXIT_SUCCESS
