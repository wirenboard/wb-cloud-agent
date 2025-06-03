#!/usr/bin/env python3
import argparse
import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from functools import cache
from json import JSONDecodeError
from string import Template
from urllib.parse import urljoin, urlparse

from tabulate import tabulate

from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.settings import (
    AppSettings,
    generate_config,
    get_providers,
    load_providers_configs,
)
from wb.cloud_agent.version import package_version

HTTP_200_OK = 200
HTTP_204_NO_CONTENT = 204

DEFAULT_CONF_DIR = "/etc"
PROVIDERS_CONF_DIR = "/etc/wb-cloud-agent/providers"
DIAGNOSTIC_DIR = "/tmp"

CLIENT_CERT_ERROR_MSG = (
    "Cert {cert_file} and key {cert_engine_key} "
    "seem to be inconsistent (possibly because of CPU board missmatch)!"
)


def start_service(service: str, restart=False):
    subprocess.run(["systemctl", "enable", service], check=True)
    if restart:
        print(f"Restarting service {service}")
        subprocess.run(["systemctl", "restart", service], check=True)
    else:
        print(f"Starting service {service}")
        subprocess.run(["systemctl", "start", service], check=True)


def setup_log(settings: AppSettings):
    numeric_level = getattr(logging, settings.LOG_LEVEL.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {settings.LOG_LEVEL}")
    logging.basicConfig(level=numeric_level, encoding="utf-8", format="%(message)s")


def update_providers_list(mqtt):
    #  Find a better way to update providers list (services enabled? services running?).
    mqtt.publish_providers(",".join(get_providers()))


def do_curl(settings: AppSettings, method="get", endpoint="", params=None):
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

    url = settings.CLOUD_AGENT_URL + endpoint

    command += [
        "--connect-timeout",
        "45",
        "--retry",
        "8",
        "--retry-delay",
        "1",
        "--retry-all-errors",
        "--cert",
        settings.CLIENT_CERT_FILE,
        "--key",
        settings.CLIENT_CERT_ENGINE_KEY,
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
                    cert_file=settings.CLIENT_CERT_FILE, cert_engine_key=settings.CLIENT_CERT_ENGINE_KEY
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
        status = int(json.loads(split_result[1])["code"])
    except (KeyError, TypeError, ValueError, JSONDecodeError) as e:
        raise ValueError(f"Invalid data in response: {split_result}") from e

    return data, status


def write_to_file(fpath: str, contents: str):
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    with open(fpath, mode="w", encoding="utf-8") as file:
        file.write(contents)


def write_activation_link(settings: AppSettings, link, mqtt):
    write_to_file(fpath=settings.ACTIVATION_LINK_CONFIG, contents=link)
    mqtt.publish_ctrl("activation_link", link)


def read_activation_link(settings: AppSettings):
    if not os.path.exists(settings.ACTIVATION_LINK_CONFIG):
        return "unknown"
    with open(settings.ACTIVATION_LINK_CONFIG, "r", encoding="utf-8") as file:
        return file.readline()


def update_activation_link(settings: AppSettings, payload, mqtt):
    write_activation_link(settings, payload["activationLink"], mqtt)


def update_tunnel_config(settings: AppSettings, payload, mqtt):
    write_to_file(fpath=settings.FRP_CONFIG, contents=payload["config"])
    start_service(settings.FRP_SERVICE, restart=True)
    write_activation_link(settings, "unknown", mqtt)


def update_metrics_config(settings: AppSettings, payload, mqtt):
    write_to_file(
        fpath=settings.TELEGRAF_CONFIG,
        contents=Template(payload["config"]).safe_substitute(BROKER_URL=settings.BROKER_URL),
    )
    start_service(settings.TELEGRAF_SERVICE, restart=True)
    write_activation_link(settings, "unknown", mqtt)


def upload_diagnostic(settings: AppSettings):
    files = sorted(glob.glob(os.path.join(DIAGNOSTIC_DIR, "diag_*.zip")), key=os.path.getmtime)
    if not files:
        logging.error("No diagnostics collected")
        _, http_status = do_curl(
            settings=settings, method="put", endpoint="diagnostic-status/", params={"status": "error"}
        )
        if http_status != HTTP_200_OK:
            logging.error("Not a 200 status while updating diagnostic status: %s", http_status)
        return

    last_diagnostic = files[-1]
    logging.info("Diagnostics collected: %s", last_diagnostic)
    _data, http_status = do_curl(
        settings=settings, method="multipart-post", endpoint="upload-diagnostic/", params=last_diagnostic
    )
    if http_status != HTTP_200_OK:
        logging.error("Not a 200 status while making upload_diagnostic request: %s", http_status)

    os.remove(last_diagnostic)


def fetch_diagnostics(settings: AppSettings, _payload, _mqtt):
    # remove old diagnostics
    try:
        for fname in glob.glob(f"{DIAGNOSTIC_DIR}/diag_*.zip"):
            os.remove(fname)
    except OSError as e:
        logging.warning("Erase diagnostic files failed: %s", e.strerror)

    def process_waiter():
        with subprocess.Popen(
            "wb-diag-collect diag",
            cwd=DIAGNOSTIC_DIR,
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


def make_event_request(settings: AppSettings, mqtt):
    event_data, http_status = do_curl(settings=settings, method="get", endpoint="events/")
    logging.debug("Checked for new events. Status %s. Data: %s", http_status, event_data)

    if http_status == HTTP_204_NO_CONTENT:
        return

    if http_status != HTTP_200_OK:
        raise ValueError("Not a 200 status while retrieving event: " + str(http_status))

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

    logging.info("Event '%s' handled successfully, event id %s", code, event_id)

    _, http_status = do_curl(settings=settings, method="post", endpoint="events/" + event_id + "/confirm/")

    if http_status != HTTP_204_NO_CONTENT:
        raise ValueError("Not a 204 status on event confirmation: " + str(http_status))


def make_start_up_request(settings: AppSettings, mqtt):
    status_data, http_status = do_curl(settings=settings, method="get", endpoint="agent-start-up/")
    if http_status != HTTP_200_OK:
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
    if http_status != HTTP_200_OK:
        logging.error("Not a 200 status while making send_agent_version request: %s", http_status)


def on_message(userdata, message):
    _status_data, http_status = do_curl(
        userdata.get("settings"),
        method="put",
        endpoint="update_device_data/",
        params={"hardware_revision": str(message.payload, "utf-8")},
    )
    if http_status != HTTP_200_OK:
        raise ValueError("Not a 200 status while making start up request: " + str(http_status))


def validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(f"Invalid URL: {value}")
    return value


def parse_args():
    main_parser = argparse.ArgumentParser()
    main_parser.add_argument("--daemon", action="store_true", help="Run cloud agent in daemon mode")
    main_parser.add_argument("--broker", help="MQTT broker url")
    subparsers = main_parser.add_subparsers(title="Actions", help="Choose mode:\n", required=False)
    change_provider_parser = subparsers.add_parser("change-provider", help="Add new cloud service provider")
    change_provider_parser.add_argument("provider_name", help="Cloud Provider name to add", default="default")
    change_provider_parser.add_argument(
        "base_url",
        type=validate_url,
        help="Cloud Provider base URL, e.g. https://wirenboard.cloud",
        nargs="?",  # not required
        default=AppSettings.CLOUD_BASE_URL,
    )
    change_provider_parser.set_defaults(func=change_provider)
    options = main_parser.parse_args()
    return options


def change_provider(options, mqtt):
    providers = get_providers()
    if options.provider_name in providers:
        provider_base_url = load_providers_configs(providers)[options.provider_name].get(
            "CLOUD_BASE_URL", AppSettings.CLOUD_BASE_URL
        )
        print(f"Provider {options.provider_name} with url {provider_base_url} already exists")
        return 1

    generate_config(options.provider_name, options.base_url)
    start_service(f"wb-cloud-agent@{options.provider_name}.service")
    update_providers_list(mqtt)
    print(f"Provider {options.provider_name} with url {options.base_url} successfully added")
    return 0


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


def get_base_url_from_cfg(cfg: dict) -> str:
    cfg_url_key = "CLOUD_BASE_URL"
    return cfg.get(cfg_url_key, AppSettings.CLOUD_BASE_URL)


def show_providers_table(providers_configs: dict[str, dict[str, str]]) -> None:
    table = []
    for name, cfg in providers_configs.items():
        base_url = get_base_url_from_cfg(cfg)
        controller_url = get_controller_url(base_url)
        table.append([name, controller_url])

    headers = ["Provider", "Controller Url"]
    print(tabulate(table, headers=headers, tablefmt="grid"))


def show_activation_link(settings: AppSettings) -> None:
    link = read_activation_link(settings)
    if link != "unknown":
        print(f"Link for connect controller to cloud:\n{link}")
    else:
        providers = get_providers()
        providers_configs = load_providers_configs(providers)
        print("Connected providers:")
        show_providers_table(providers_configs)


def run_daemon(mqtt, settings):
    mqtt.publish_vdev()
    mqtt.publish_ctrl("activation_link", read_activation_link(settings))
    mqtt.publish_ctrl("cloud_base_url", settings.CLOUD_BASE_URL)
    mqtt.publish_ctrl("status", "connecting")
    with ExitStack() as stack:
        stack.callback(mqtt.remove_vdev)
        while True:
            start = time.perf_counter()
            try:
                make_event_request(settings, mqtt)
            except subprocess.TimeoutExpired:
                continue
            except Exception:  # pylint:disable=broad-exception-caught
                err_msg = "Error making request to cloud!"
                logging.exception(err_msg)
                mqtt.publish_ctrl("status", err_msg)
            else:
                mqtt.publish_ctrl("status", "ok")
            request_time = time.perf_counter() - start
            logging.debug("Done in: %s ms.", int(request_time * 1000))
            time.sleep(settings.REQUEST_PERIOD_SECONDS)


def main():
    options = parse_args()
    cloud_provider = getattr(options, "provider_name", "default")
    try:
        settings = AppSettings(cloud_provider)
    except (FileNotFoundError, OSError, json.decoder.JSONDecodeError):
        return 6  # systemd status=6/NOTCONFIGURED

    setup_log(settings)

    settings.BROKER_URL = options.broker or settings.BROKER_URL

    mqtt = MQTTCloudAgent(settings, on_message)

    if not options.daemon:
        if hasattr(options, "func"):
            mqtt.start()
            return options.func(options, mqtt)

        mqtt.start()
        make_start_up_request(settings, mqtt)
        return show_activation_link(settings)

    try:
        mqtt.start(update_status=True)
    except Exception as ex:  # pylint:disable=broad-exception-caught
        logging.error("Error starting MQTT client: %s", ex)
    make_start_up_request(settings, mqtt)
    send_agent_version(settings)
    update_providers_list(mqtt)

    run_daemon(mqtt, settings)
    return 0
