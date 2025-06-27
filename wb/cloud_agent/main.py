#!/usr/b#!/usr/bin/env python3
import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from argparse import Namespace
from contextlib import ExitStack
from functools import cache
from http import HTTPStatus as status
from json import JSONDecodeError
from pathlib import Path
from string import Template
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from tabulate import tabulate

from wb.cloud_agent import __version__ as package_version
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.settings import (
    PROVIDERS_CONF_DIR,
    AppSettings,
    delete_provider_config,
    generate_provider_config,
    get_providers,
    load_providers_activation_links,
    load_providers_configs,
)

CLIENT_CERT_ERROR_MSG = (
    "Cert {cert_file} and key {cert_engine_key} "
    "seem to be inconsistent (possibly because of CPU board missmatch)!"
)


def start_service(service: str, restart: bool = False) -> None:
    logging.debug("Enabling service %s", service)

    result = subprocess.run(
        ["systemctl", "enable", service],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.stdout:
        logging.debug("stdout: %s", result.stdout.strip())
    if result.stderr:
        logging.debug("stderr: %s", result.stderr.strip())

    if restart:
        logging.debug("Restarting service %s", service)
        subprocess.run(["systemctl", "restart", service], check=True)
    else:
        logging.debug("Starting service %s", service)
        subprocess.run(["systemctl", "start", service], check=True)


def stop_service(service: str) -> None:
    logging.debug("Disabling service %s", service)

    result = subprocess.run(
        ["systemctl", "disable", service],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.stdout:
        logging.debug("stdout: %s", result.stdout.strip())
    if result.stderr:
        logging.debug("stderr: %s", result.stderr.strip())

    logging.debug("Stopping service %s", service)
    subprocess.run(["systemctl", "stop", service], check=True)


def stop_services_and_del_configs(provider_name: str) -> None:
    stop_service(f"wb-cloud-agent@{provider_name}.service")
    stop_service(f"wb-cloud-agent-frpc@{provider_name}.service")
    stop_service(f"wb-cloud-agent-telegraf@{provider_name}.service")
    delete_provider_config(PROVIDERS_CONF_DIR, provider_name)
    print(f"Provider {provider_name} successfully deleted")


def setup_log(settings: AppSettings) -> None:
    numeric_level = getattr(logging, settings.log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {settings.log_level}")
    logging.basicConfig(level=numeric_level, encoding="utf-8", format="%(message)s")


def update_providers_list(mqtt: MQTTCloudAgent) -> None:
    #  Find a better way to update providers list (services enabled? services running?).
    mqtt.publish_providers(",".join(get_providers()))


def do_curl(
    settings: AppSettings, method: str = "get", endpoint: str = "", params: Optional[dict] = None
) -> tuple[dict, int]:
    data_delimiter = "|||"
    output_format = data_delimiter + '{"code":"%{response_code}"}'

    if method == "get":
        command = ["curl"]
    elif method in ("post", "put"):
        command = ["curl", "-X", method.upper()]
        if params:
            command += ["-H", "Content-Type: application/json", "-d", json.dumps(params)]
    elif method == "multipart-post":
        command = ["curl", "-X", "POST", "-F", f"file=@{params}"]
    else:
        raise ValueError("Invalid method: " + method)

    url = settings.cloud_agent_url + endpoint

    command += [
        "--connect-timeout",
        "45",
        "--retry",
        "8",
        "--retry-delay",
        "1",
        "--retry-all-errors",
        "--cert",
        settings.client_cert_file,
        "--key",
        settings.client_cert_engine_key,
        "--engine",
        "ateccx08",
        "--key-type",
        "ENG",
        "-w",
        output_format,
        url,
    ]

    try:
        result = subprocess.run(command, timeout=360, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        if e.returncode == 58:
            raise RuntimeError(
                CLIENT_CERT_ERROR_MSG.format(
                    cert_file=settings.client_cert_file, cert_engine_key=settings.client_cert_engine_key
                )
            ) from e
        raise e

    decoded_result = result.stdout.decode("utf-8")
    split_result = decoded_result.split(data_delimiter)
    if len(split_result) != 2:
        raise ValueError("Invalid data in response: " + str(split_result))

    try:
        data = json.loads(split_result[0])
    except JSONDecodeError:
        data = {}

    try:
        status_code = int(json.loads(split_result[1])["code"])
    except (KeyError, TypeError, ValueError, JSONDecodeError) as e:
        raise ValueError(f"Invalid data in response: {split_result}") from e

    return data, status_code


def write_to_file(fpath: Path, contents: str) -> None:
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(contents, encoding="utf-8")


def write_activation_link(settings: AppSettings, link: str, mqtt: MQTTCloudAgent) -> None:
    logging.debug("Write activation link %s to %s", link, settings.activation_link_config)
    write_to_file(fpath=settings.activation_link_config, contents=link)
    mqtt.publish_ctrl("activation_link", link)


def read_activation_link(settings: AppSettings) -> str:
    logging.debug("Read activation link from %s", settings.activation_link_config)

    if not settings.activation_link_config.exists():
        return "unknown"

    activation_link = settings.activation_link_config.read_text(encoding="utf-8").strip()

    logging.debug("Readed activation link %s", activation_link)
    return activation_link


def update_activation_link(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    write_activation_link(settings, payload["activationLink"], mqtt)


def update_tunnel_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    write_to_file(fpath=settings.frp_config, contents=payload["config"])
    start_service(settings.frp_service, restart=True)
    write_activation_link(settings, "unknown", mqtt)


def update_metrics_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    write_to_file(
        fpath=settings.telegraf_config,
        contents=Template(payload["config"]).safe_substitute(BROKER_URL=settings.broker_url),
    )
    start_service(settings.telegraf_service, restart=True)
    write_activation_link(settings, "unknown", mqtt)


def upload_diagnostic(settings: AppSettings) -> None:
    files = sorted(settings.diag_archive.glob("diag_*.zip"), key=lambda p: p.stat().st_mtime)
    if not files:
        logging.error("No diagnostics collected")

        _, http_status = do_curl(
            settings=settings, method="put", endpoint="diagnostic-status/", params={"status": "error"}
        )
        if http_status != status.OK:
            logging.error("Not a 200 status while updating diagnostic status: %s", http_status)
        return

    last_diagnostic = files[-1]
    logging.info("Diagnostics collected: %s", last_diagnostic)

    _data, http_status = do_curl(
        settings=settings, method="multipart-post", endpoint="upload-diagnostic/", params=last_diagnostic
    )
    if http_status != status.OK:
        logging.error("Not a 200 status while making upload_diagnostic request: %s", http_status)

    last_diagnostic.unlink()


def fetch_diagnostics(settings: AppSettings, _payload, _mqtt):
    # remove old diagnostics
    try:
        for fname in settings.diag_archive.glob("diag_*.zip"):
            fname.unlink()
    except OSError as e:
        logging.warning("Erase diagnostic files failed: %s", e.strerror)

    def process_waiter():
        with subprocess.Popen(
            "wb-diag-collect diag",
            cwd=settings.diag_archive,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            process.wait()

        upload_diagnostic(settings)

    thread = threading.Thread(target=process_waiter)
    thread.start()


HANDLERS = {
    "update_activation_link": update_activation_link,
    "update_tunnel_config": update_tunnel_config,
    "update_metrics_config": update_metrics_config,
    "fetch_diagnostics": fetch_diagnostics,
}


def make_event_request(settings: AppSettings, mqtt: MQTTCloudAgent):
    event_data, http_status = do_curl(settings=settings, method="get", endpoint="events/")
    logging.debug("Checked for new events. Status %s. Data: %s", http_status, event_data)

    if http_status == status.NO_CONTENT:
        return

    if http_status != status.OK:
        raise ValueError(f"Not a 200 status while retrieving event: {http_status}")

    code = event_data.get("code", "")
    handler = HANDLERS.get(code)

    event_id = event_data.get("id")
    if not event_id:
        raise ValueError("Unknown event id: " + str(event_id))

    payload = event_data.get("payload")
    if not payload:
        raise ValueError("Empty payload")

    if handler:
        handler(settings, payload, mqtt)
    else:
        logging.warning("Got an unknown event '%s'. Try to update wb-cloud-agent package.", code)

    logging.debug("Event '%s' handled successfully, event id %s", code, event_id)

    _event_data, http_status = do_curl(
        settings=settings, method="post", endpoint="events/" + event_id + "/confirm/"
    )
    if http_status != status.NO_CONTENT:
        raise ValueError("Not a 204 status on event confirmation: " + str(http_status))


def make_start_up_request(settings: AppSettings, mqtt: MQTTCloudAgent):
    status_data, http_status = do_curl(settings=settings, method="get", endpoint="agent-start-up/")
    if http_status != status.OK:
        logging.debug("http_status=%s status_data=%s", http_status, status_data)
        raise ValueError("Not a 200 status while making start up request: " + str(http_status))

    if "activated" not in status_data or "activationLink" not in status_data:
        raise ValueError("Invalid response data while making start up request: " + str(status_data))

    activated = status_data["activated"]
    activation_link = status_data["activationLink"]

    if activated or not activation_link:
        write_activation_link(settings, "unknown", mqtt)
    else:
        write_activation_link(settings, activation_link, mqtt)

    return status_data


def send_agent_version(settings: AppSettings):
    _status_data, http_status = do_curl(
        settings=settings,
        method="put",
        endpoint="update_device_data/",
        params={"agent_version": package_version},
    )
    if http_status != status.OK:
        logging.error("Not a 200 status while making send_agent_version request: %s", http_status)


def on_message(userdata: dict, message):
    _status_data, http_status = do_curl(
        userdata.get("settings"),
        method="put",
        endpoint="update_device_data/",
        params={"hardware_revision": str(message.payload, "utf-8")},
    )
    if http_status != status.OK:
        raise ValueError("Not a 200 status while making start up request: " + str(http_status))


@cache
def get_ctrl_serial_number() -> str:
    try:
        return subprocess.check_output("wb-gen-serial -s", shell=True).decode().strip()
    except FileNotFoundError:
        print("Command wb-gen-serial not found on controller.")
        sys.exit(1)


def get_controller_url(base_url: str) -> str:
    ctrl_serial_number = get_ctrl_serial_number()
    return urljoin(base_url, f"controllers/{ctrl_serial_number}")


def merge_providers_configs_with_links(
    providers_configs: dict[str, dict[str, str]], providers_links: dict[str, Any]
) -> dict[str, str]:
    providers_with_urls = {}

    providers = list(providers_configs) + [
        provider for provider in providers_links if provider not in providers_configs
    ]

    for provider in providers:
        val1 = providers_configs.get(provider)
        val2 = providers_links.get(provider)

        if isinstance(val2, str) and val2.startswith("http"):
            providers_with_urls[provider] = val2
        elif isinstance(val1, dict):
            providers_with_urls[provider] = get_controller_url(val1["CLOUD_BASE_URL"])

    return providers_with_urls


def show_providers_table(providers_with_urls: dict[str, str]) -> None:
    if not providers_with_urls:
        print("No one provider was found")
        return

    table = []
    for provider_name, url in providers_with_urls.items():
        table.append([provider_name, url])

    headers = ["Provider", "Controller Url / Activation Url"]
    print(tabulate(table, headers=headers, tablefmt="github"))


def configure_app(provider_name: str) -> AppSettings:
    try:
        settings = AppSettings(provider_name)
    except (FileNotFoundError, OSError, json.decoder.JSONDecodeError):
        return 6  # systemd status=6/NOTCONFIGURED

    setup_log(settings)
    return settings


def validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(f"Invalid URL: {value}")
    return value


def parse_args() -> Namespace:
    main_parser = argparse.ArgumentParser()
    main_parser.set_defaults(func=show_providers)

    subparsers = main_parser.add_subparsers(
        dest="command", title="Actions", help="Choose mode:\n", required=False
    )

    add_provider_parser = subparsers.add_parser("add-provider", help="Add new cloud service provider")
    add_provider_parser.add_argument(
        "base_url",
        type=validate_url,
        help="Cloud Provider base URL, e.g. https://wirenboard.cloud",
    )
    add_provider_parser.add_argument(
        "--name", help="Cloud Provider name to add (override url hostname)", required=False
    )
    add_provider_parser.set_defaults(func=add_provider)

    add_on_premise_provider_parser = subparsers.add_parser(
        "use-on-premise", help="Add new cloud service provider"
    )
    add_on_premise_provider_parser.add_argument(
        "base_url",
        type=validate_url,
        help="On-Premise Cloud Provider base URL, e.g. https://on-premise.cloud",
    )
    add_on_premise_provider_parser.add_argument(
        "--name", help="On-Premise Cloud Provider name to add (override url hostname)", required=False
    )
    add_on_premise_provider_parser.set_defaults(func=add_on_premise_provider)

    del_provider_parser = subparsers.add_parser("del-provider", help="Delete cloud service provider")
    del_provider_parser.add_argument(
        "provider_name",
        help="Cloud Provider name to delete",
    )
    del_provider_parser.set_defaults(func=del_provider)

    del_all_providers_parser = subparsers.add_parser(
        "del-all-providers", help="Delete all cloud service providers"
    )
    del_all_providers_parser.set_defaults(func=del_all_providers)

    run_daemon_parser = subparsers.add_parser("run-daemon", help="Run cloud agent in daemon mode")
    run_daemon_parser.add_argument(
        "provider_name",
        help="Cloud Provider name to run",
    )
    run_daemon_parser.add_argument("--broker", help="MQTT broker url", required=False)
    run_daemon_parser.set_defaults(func=run_daemon)

    return main_parser.parse_args()


def show_providers(_options) -> int:
    providers = get_providers()

    providers_configs = load_providers_configs(providers)
    providers_links = load_providers_activation_links(providers)

    providers_with_urls = merge_providers_configs_with_links(providers_configs, providers_links)

    show_providers_table(providers_with_urls)
    return 0


def add_provider(options) -> int:
    provider_name = options.name or urlparse(options.base_url).netloc
    settings = configure_app(provider_name)

    mqtt = MQTTCloudAgent(settings, on_message)
    mqtt.start()

    providers = get_providers()
    if provider_name in providers:
        provider_base_url = load_providers_configs(providers)[provider_name]["CLOUD_BASE_URL"]
        print(f"Provider {provider_name} with url {provider_base_url} already exists")
        return 1

    generate_provider_config(provider_name, options.base_url)
    start_service(f"wb-cloud-agent@{provider_name}.service")
    update_providers_list(mqtt)

    print(f"Provider {provider_name} successfully added")
    return 0


def add_on_premise_provider(options) -> int:
    del_all_providers(options)
    return add_provider(options)


def del_provider(options) -> int:
    provider_name = options.provider_name
    settings = configure_app(provider_name)

    mqtt = MQTTCloudAgent(settings, on_message)
    mqtt.start()

    providers = get_providers()
    if provider_name not in providers:
        print(f"Provider {provider_name} does not exists")
        return 1

    stop_services_and_del_configs(provider_name)
    update_providers_list(mqtt)
    return 0


def del_all_providers(_options) -> int:
    providers = get_providers()
    if not providers:
        print("No one provider was found")
        return 1

    for provider_name in providers:
        settings = configure_app(provider_name)

        mqtt = MQTTCloudAgent(settings, on_message)
        mqtt.start()

        stop_services_and_del_configs(provider_name)
        update_providers_list(mqtt)
    return 0


def run_daemon(options) -> int:
    settings = configure_app(options.provider_name)

    settings.broker_url = options.broker or settings.broker_url

    mqtt = MQTTCloudAgent(settings, on_message)
    try:
        mqtt.start(update_status=True)
    except Exception as ex:  # pylint:disable=broad-exception-caught
        logging.error("Error starting MQTT client: %s", ex)

    make_start_up_request(settings, mqtt)
    send_agent_version(settings)
    update_providers_list(mqtt)
    _run_daemon(mqtt, settings)
    return 0


def _run_daemon(mqtt: MQTTCloudAgent, settings: AppSettings) -> None:
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


def main() -> int:
    options = parse_args()
    return options.func(options)
