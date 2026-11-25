import logging
import signal
import subprocess
import threading
import time
from urllib.parse import urlparse

from wb.cloud_agent import __version__ as agent_package_version
from wb.cloud_agent.constants import (
    EXIT_FAILURE,
    EXIT_INVALIDARGUMENT,
    EXIT_NOTCONFIGURED,
    EXIT_SUCCESS,
    STARTUP_RETRY_MAX_PAUSE_SECONDS,
)
from wb.cloud_agent.handlers.curl import CloudNetworkError, CurlInterrupted
from wb.cloud_agent.handlers.events import event_delete_controller, make_event_request
from wb.cloud_agent.handlers.ping import wait_for_cloud_reachable
from wb.cloud_agent.handlers.startup import (
    CloudUnavailableError,
    make_start_up_request,
    on_message,
    send_packages_version,
)
from wb.cloud_agent.mqtt import MQTTCloudAgent, check_broker_url
from wb.cloud_agent.services.activation import read_activation_link
from wb.cloud_agent.services.lifecycle import stop_services_and_del_configs
from wb.cloud_agent.services.metrics import reconcile_metrics_script
from wb.cloud_agent.settings import (
    AppSettings,
    configure_app,
    generate_provider_config,
    get_provider_names,
    load_providers_data,
)
from wb.cloud_agent.utils import (
    ConfigError,
    handle_connection_state,
    normalize_base_url,
    reset_failed_service,
    show_providers_table,
    start_and_enable_service,
)


def show_providers(_options) -> int:
    provider_names = get_provider_names()
    providers = load_providers_data(provider_names)
    show_providers_table(providers)
    return 0


def settings_for_removal(provider_name: str) -> tuple[AppSettings, bool]:
    try:
        settings = configure_app(provider_name=provider_name)
    except ConfigError:
        return configure_app(provider_name=provider_name, skip_conf_file=True), False
    return settings, settings.config_file.is_file()


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
    settings, cloud_known = settings_for_removal(provider_name)

    mqtt = MQTTCloudAgent(settings, on_message)
    mqtt.start()

    providers = get_provider_names()
    if provider_name not in providers:
        print(f"Provider {provider_name} does not exists")
        return 1

    stop_services_and_del_configs(settings, provider_name, unbind=cloud_known)
    mqtt.update_providers_list()
    return 0


def del_all_providers(_options, show_msg: bool = True) -> int:
    providers = get_provider_names()
    if not providers:
        if show_msg:
            print("No one provider was found")
        return 1

    for provider_name in providers:
        settings, cloud_known = settings_for_removal(provider_name)

        mqtt = MQTTCloudAgent(settings, on_message)
        mqtt.start()

        stop_services_and_del_configs(settings, provider_name, unbind=cloud_known)
        mqtt.update_providers_list()
    return 0


def del_controller_from_cloud(options) -> int:
    settings = configure_app(provider_name="", skip_conf_file=True, cloud_base_url=options.base_url)
    return event_delete_controller(settings)


def _register_in_cloud(settings, mqtt: MQTTCloudAgent, stop_requested: threading.Event) -> bool:
    """
    Repeat the startup requests with a pause growing up to STARTUP_RETRY_MAX_PAUSE_SECONDS while the cloud
    is unreachable or answers with a temporary error; False if a stop was requested first. Other failures,
    e.g. a rejection (400/403 or a TLS alert about the client certificate) or a bad certificate (curl 58),
    are raised: a retry in process would not change them.
    """
    pause = settings.request_period_seconds
    while wait_for_cloud_reachable(settings.cloud_base_url, settings.ping_period_seconds, stop_requested):
        try:
            make_start_up_request(settings, mqtt)
            send_packages_version(settings)
            return True
        except (CloudNetworkError, CloudUnavailableError, CurlInterrupted, subprocess.TimeoutExpired) as exc:
            logging.error("Startup request failed: %s. Retrying in %s s...", exc, pause)
            mqtt.publish_ctrl("status", _retry_status(exc))
            stop_requested.wait(pause)
            pause = min(pause * 2, STARTUP_RETRY_MAX_PAUSE_SECONDS)
    return False


def _retry_status(error: Exception) -> str:
    if isinstance(error, CloudUnavailableError):
        return f"Cloud is unavailable (HTTP {error.http_status})! Retrying..."
    return "Network or Cloud is unreachable! Retrying..."


def _serve_cloud(settings, mqtt: MQTTCloudAgent, stop_requested: threading.Event) -> int:
    """
    Publish the virtual device, register in the cloud and poll its events until a stop is requested.

    Exit code 1 when the cloud rejects the registration: systemd restarts the unit, and check-certs.sh
    rebuilds the certificate before the next attempt. A successful registration resets the restart counter
    of the unit, so the RestartSteps delay of a later failure starts from RestartSec again.
    """
    mqtt.publish_ctrl("status", "starting")
    mqtt.update_providers_list()
    mqtt.publish_vdev()
    mqtt.publish_ctrl("activation_link", read_activation_link(settings))
    mqtt.publish_ctrl("cloud_base_url", settings.cloud_base_url)

    try:
        if not _register_in_cloud(settings, mqtt, stop_requested):
            return EXIT_SUCCESS
    except Exception as exc:  # pylint:disable=broad-exception-caught
        logging.error("Startup request failed: %s", exc)
        return EXIT_FAILURE

    reset_failed_service(f"wb-cloud-agent@{settings.provider_name}.service")
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

        except CurlInterrupted as exc:
            # on a stop the loop ends at the wait below anyway; a curl killed by anyone else is retried
            conn_state, msg, exc_info = False, "Request interrupted", exc

        except Exception:  # pylint:disable=broad-exception-caught
            logging.exception("Cloud connection exception")
            conn_state, msg, exc_info = False, "Error making request to cloud! Retrying...", None

        was_connected = handle_connection_state(was_connected, conn_state, msg, mqtt)

        if exc_info is not None:
            logging.debug(msg, exc_info=exc_info)

        logging.debug("Event request completed in %s ms", int((time.perf_counter() - start) * 1000))
        stop_requested.wait(settings.request_period_seconds)
    return EXIT_SUCCESS


def run_daemon(options) -> int:
    # A missing or damaged wirenboard.cloud config at the default path is restored first;
    # any other unusable config raises ConfigError, which main() turns into exit code 6.
    settings = configure_app(provider_name=options.provider_name, recover_configs=True)
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

    # The broker is not needed to serve the cloud: remote diagnostics and tunnels must work while
    # mosquitto is down. paho keeps connecting in the background, and the state set meanwhile is
    # published once the broker answers.
    mqtt = MQTTCloudAgent(settings, on_message, stop_requested)
    mqtt.start(daemon=True)
    exit_code = _serve_cloud(settings, mqtt, stop_requested)
    if mqtt.authentication_failed:
        exit_code = EXIT_INVALIDARGUMENT
    if exit_code != EXIT_SUCCESS:
        mqtt.publish_stopped()
        mqtt.stop()
        return exit_code

    # Only the agent itself can clear its retained topics, so this runs on a requested stop only.
    # The Last Will marks the status as stopped only when the connection breaks, i.e. after a crash;
    # every other exit reports its stop itself.
    mqtt.remove_vdev()
    mqtt.stop()
    logging.info("Cloud Agent stopped")
    return EXIT_SUCCESS
