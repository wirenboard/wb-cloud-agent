import logging
import signal
import subprocess
import threading
import time
from urllib.parse import urlparse

from wb.cloud_agent import __version__ as agent_package_version
from wb.cloud_agent.constants import (
    EXIT_FAILURE,
    EXIT_INVALID_ARGUMENT,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
)
from wb.cloud_agent.handlers.curl import CloudNetworkError
from wb.cloud_agent.handlers.events import event_delete_controller, make_event_request
from wb.cloud_agent.handlers.ping import wait_for_cloud_reachable
from wb.cloud_agent.handlers.startup import (
    make_start_up_request,
    send_hardware_revision,
    send_packages_version,
)
from wb.cloud_agent.mqtt import MQTTCloudAgent
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
        mqtt = MQTTCloudAgent(settings)
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

    mqtt = MQTTCloudAgent(settings)
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

        mqtt = MQTTCloudAgent(settings)
        mqtt.start()

        stop_services_and_del_configs(settings, provider_name)
        mqtt.update_providers_list()
    return 0


def del_controller_from_cloud(options) -> int:
    settings = configure_app(provider_name="", skip_conf_file=True, cloud_base_url=options.base_url)
    return event_delete_controller(settings)


def run_daemon(options) -> int:
    stop_requested = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_args: stop_requested.set())
    try:
        settings = configure_app(
            provider_name=options.provider_name, config_file=options.config, require_conf_file=True
        )
    except SystemExit as error:
        return EXIT_SUCCESS if stop_requested.is_set() else error.code
    if stop_requested.is_set():
        return EXIT_SUCCESS
    settings.broker_url = options.broker or settings.broker_url
    logging.info(
        "====== Cloud Agent started (version: %s, provider: %s) ======",
        agent_package_version,
        settings.cloud_base_url,
    )

    try:
        mqtt = MQTTCloudAgent(settings)
    except ValueError as error:
        logging.error("Invalid MQTT broker configuration: %s", error)
        return EXIT_INVALID_ARGUMENT if options.broker else EXIT_NOT_CONFIGURED
    try:
        if mqtt.connect(stop_requested):
            _run_cloud(settings, mqtt, stop_requested)
        return EXIT_SUCCESS
    except PermissionError as error:
        logging.error("%s", error)
        return EXIT_SUCCESS if stop_requested.is_set() else EXIT_INVALID_ARGUMENT
    except Exception as exc:  # pylint:disable=broad-exception-caught
        logging.error("Cloud agent failed: %s", exc)
        return EXIT_SUCCESS if stop_requested.is_set() else EXIT_FAILURE
    finally:
        try:
            mqtt.remove_vdev()
        finally:
            mqtt.stop()


def _send_startup_requests(settings, mqtt, stop_requested: threading.Event) -> bool:
    make_start_up_request(settings, mqtt)
    if stop_requested.is_set():
        return False
    send_packages_version(settings)
    return not stop_requested.is_set()


def _run_cloud(settings, mqtt, stop_requested: threading.Event) -> None:
    mqtt.publish_ctrl("status", "starting")
    mqtt.update_providers_list()
    mqtt.publish_vdev()
    mqtt.publish_ctrl("activation_link", read_activation_link(settings))
    mqtt.publish_ctrl("cloud_base_url", settings.cloud_base_url)
    if not wait_for_cloud_reachable(settings.cloud_base_url, settings.ping_period_seconds, stop_requested):
        return

    initialized = False
    was_connected = False
    hardware_revision = None
    while not stop_requested.is_set():
        start = time.perf_counter()
        try:
            if not initialized and not _send_startup_requests(settings, mqtt, stop_requested):
                break
            if mqtt.hardware_revision is not None and mqtt.hardware_revision != hardware_revision:
                revision = mqtt.hardware_revision
                send_hardware_revision(settings, revision)
                hardware_revision = revision
            if not initialized and not stop_requested.is_set():
                reconcile_metrics_script(settings)
                initialized = True
                mqtt.publish_ctrl("status", "connecting")
                logging.info("Cloud Agent initialization - OK")
            if not stop_requested.is_set():
                make_event_request(settings, mqtt)
            conn_state, msg = True, "Cloud Agent is successfully connected to the cloud!"
        except subprocess.TimeoutExpired:
            conn_state, msg = False, "Request timeout. Retrying..."
        except CloudNetworkError:
            conn_state, msg = False, "Network or Cloud is unreachable! Retrying..."
        except Exception:  # pylint:disable=broad-exception-caught
            logging.exception("Cloud request failed")
            conn_state, msg = False, "Error making request to cloud! Retrying..."
        if initialized:
            was_connected = handle_connection_state(was_connected, conn_state, msg, mqtt)
        elif not conn_state:
            logging.warning("Cloud startup incomplete: %s", msg)
        logging.debug("Cloud request completed in %s ms", int((time.perf_counter() - start) * 1000))
        stop_requested.wait(settings.request_period_seconds)
