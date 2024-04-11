#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
import time
from contextlib import ExitStack
from json import JSONDecodeError

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.version import package_version

HTTP_200_OK = 200
HTTP_204_NO_CONTENT = 204

DEFAULT_CONF_DIR = "/mnt/data/etc"
PROVIDERS_CONF_DIR = "/mnt/data/etc/wb-cloud-agent/providers"


class AppSettings:
    """
    Simple settings configurator.

    To rewrite parameters just add them to wb-cloud-agent config.

    An example of config at /mnt/data/etc/wb-cloud-agent.conf:

    {
        "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:04:C0:00",
    }

    """

    LOG_LEVEL: str = "INFO"

    CLIENT_CERT_ENGINE_KEY: str = "ATECCx08:00:02:C0:00"
    CLIENT_CERT_FILE: str = "/var/lib/wb-cloud-agent/device_bundle.crt.pem"
    CLOUD_BASE_URL: str = "https://wirenboard.cloud"
    CLOUD_AGENT_URL: str = "https://agent.wirenboard.cloud/api-agent/v1/"
    REQUEST_PERIOD_SECONDS: int = 3

    def __init__(self, provider: str, conf_file=None):
        self.PROVIDER = provider
        if provider == "default":
            self.FRP_SERVICE: str = "wb-cloud-agent-frpc.service"
            self.TELEGRAF_SERVICE: str = "wb-cloud-agent-telegraf.service"
            self.FRP_CONFIG: str = f"/var/lib/wb-cloud-agent/frpc.conf"
            self.TELEGRAF_CONFIG: str = f"/var/lib/wb-cloud-agent/telegraf.conf"
            self.ACTIVATION_LINK_CONFIG: str = f"/var/lib/wb-cloud-agent/activation_link.conf"
        else:
            self.FRP_SERVICE: str = f"wb-cloud-agent-frpc@{provider}.service"
            self.TELEGRAF_SERVICE: str = f"wb-cloud-agent-telegraf@{provider}.service"
            self.FRP_CONFIG: str = f"/var/lib/wb-cloud-agent/providers/{provider}/frpc.conf"
            self.TELEGRAF_CONFIG: str = f"/var/lib/wb-cloud-agent/providers/{provider}/telegraf.conf"
            self.ACTIVATION_LINK_CONFIG: str = (
                f"/var/lib/wb-cloud-agent/providers/{provider}/activation_link.conf"
            )
        self.MQTT_PREFIX: str = f"/devices/system__wb-cloud-agent__{provider}"
        if conf_file:
            self.apply_conf_file(conf_file)

    def apply_conf_file(self, conf_file):
        conf = read_json_file(conf_file)
        for key in conf:
            setattr(self, key, conf[key])


def read_json_file(file_path):
    try:
        with open(file_path, "r") as file:
            return json.load(file)
    except (FileNotFoundError, OSError, JSONDecodeError):
        raise ValueError("Cannot read config file at: " + file_path)
    except JSONDecodeError:
        raise ValueError("Invalid config file format (must be valid json) at: " + file_path)


def start_service(service: str, restart=False):
    subprocess.run(["systemctl", "enable", service], check=True)
    if restart:
        print(f"Restarting service {service}")
        subprocess.run(["systemctl", "restart", service], check=True)
    else:
        print(f"Starting service {service}")
        subprocess.run(["systemctl", "start", service], check=True)


def config_file_path(provider: str):
    if provider == "default":
        return f"{DEFAULT_CONF_DIR}/wb-cloud-agent.conf"
    return f"{PROVIDERS_CONF_DIR}/{provider}/wb-cloud-agent.conf"


def setup_log(settings: AppSettings):
    numeric_level = getattr(logging, settings.LOG_LEVEL.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid log level: %s" % settings.LOG_LEVEL)
    logging.basicConfig(level=numeric_level, encoding="utf-8", format="%(message)s")


def update_providers_list(settings: AppSettings, mqtt):
    #  FIXME: Find a better way to update providers list (services enabled? services running?).
    providers = ["default"]
    if not os.path.exists(PROVIDERS_CONF_DIR):
        return providers
    providers += [
        d for d in os.listdir(PROVIDERS_CONF_DIR) if os.path.isdir(os.path.join(PROVIDERS_CONF_DIR, d))
    ]
    mqtt.publish("/wb-cloud-agent/providers", ",".join(providers), retain=True, qos=2)


def do_curl(settings: AppSettings, method="get", endpoint="", body=None):
    data_delimiter = "|||"
    output_format = data_delimiter + '{"code":"%{response_code}"}'

    if method == "get":
        command = ["curl"]
    elif method in ("post", "put"):
        command = ["curl", "-X", method.upper()]
        if body:
            command += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    else:
        raise ValueError("Invalid method: " + method)

    url = settings.CLOUD_AGENT_URL + endpoint

    command += [
        "--connect-timeout",
        "45",
        "--retry",
        "8",
        "--retry-max-time",
        "300",
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

    result = subprocess.run(command, timeout=360, check=True, capture_output=True)

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
    except (KeyError, TypeError, ValueError, JSONDecodeError):
        raise ValueError("Invalid data in response: " + str(split_result))

    return data, status


def write_activation_link(settings: AppSettings, link, mqtt):
    with open(settings.ACTIVATION_LINK_CONFIG, "w") as file:
        file.write(link)
    publish_ctrl(settings, mqtt, "activation_link", link)


def read_activation_link(settings: AppSettings):
    if not os.path.exists(settings.ACTIVATION_LINK_CONFIG):
        return "unknown"
    with open(settings.ACTIVATION_LINK_CONFIG, "r") as file:
        return file.readline()


def update_activation_link(settings: AppSettings, payload, mqtt):
    write_activation_link(settings, payload["activationLink"], mqtt)


def update_tunnel_config(settings: AppSettings, payload, mqtt):
    with open(settings.FRP_CONFIG, "w") as file:
        file.write(payload["config"])
    start_service(settings.FRP_SERVICE, restart=True)
    write_activation_link(settings, "unknown", mqtt)


def update_metrics_config(settings: AppSettings, payload, mqtt):
    with open(settings.TELEGRAF_CONFIG, "w") as file:
        file.write(payload["config"])
    start_service(settings.TELEGRAF_SERVICE, restart=True)
    write_activation_link(settings, "unknown", mqtt)


HANDLERS = {
    "update_activation_link": update_activation_link,
    "update_tunnel_config": update_tunnel_config,
    "update_metrics_config": update_metrics_config,
}


def publish_vdev(settings: AppSettings, mqtt):
    mqtt.publish(settings.MQTT_PREFIX + "/meta/name", "cloud status", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/meta/driver", "wb-cloud-agent", retain=True, qos=2)
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/status/meta",
        '{"type": "text", "readonly": true, "order": 1, "title": {"en": "Status"}}',
        retain=True,
        qos=2,
    )
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/activation_link/meta",
        '{"type": "text", "readonly": true, "order": 2, "title": {"en": "Link"}}',
        retain=True,
        qos=2,
    )
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/cloud_base_url/meta",
        '{"type": "text", "readonly": true, "order": 3, "title": {"en": "URL"}}',
        retain=True,
        qos=2,
    )
    mqtt.publish(settings.MQTT_PREFIX + "/controls/status", "disconnected", retain=True, qos=2)
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/activation_link", read_activation_link(settings), retain=True, qos=2
    )
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/cloud_base_url", settings.CLOUD_BASE_URL, retain=True, qos=2
    )


def remove_vdev(settings: AppSettings, mqtt):
    mqtt.publish(settings.MQTT_PREFIX + "/meta/name", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/meta/driver", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/status/meta", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/activation_link/meta", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/cloud_base_url/meta", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/status", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/activation_link", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/cloud_base_url", "", retain=True, qos=2)


def publish_ctrl(settings: AppSettings, mqtt, ctrl, value):
    mqtt.publish(settings.MQTT_PREFIX + f"/controls/{ctrl}", value, retain=True, qos=2)


def make_event_request(settings: AppSettings, mqtt):
    event_data, http_status = do_curl(settings=settings, method="get", endpoint="events/")
    logging.debug("Checked for new events. Status " + str(http_status) + ". Data: " + str(event_data))

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
        logging.warning("Got an unknown event '" + code + "'. Try to update wb-cloud-agent package.")

    logging.info("Event '" + code + "' handled successfully, event id " + str(event_id))

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
    status_data, http_status = do_curl(
        settings=settings,
        method="put",
        endpoint="update_device_data/",
        body={"agent_version": package_version},
    )
    if http_status != HTTP_200_OK:
        logging.error("Not a 200 status while making send_agent_version request: " + str(http_status))


def on_connect(client, _, flags, reason_code, properties=None):
    # 0: Connection successful
    if reason_code != 0:
        logging.error(f"Failed to connect: {reason_code}. loop_forever() will retry connection")
    else:
        client.subscribe("/devices/system/controls/HW Revision", qos=2)


def on_message(client, userdata, message):
    assert "settings" in userdata, "No settings in userdata"
    client.unsubscribe("/devices/system/controls/HW Revision")
    status_data, http_status = do_curl(
        userdata.get("settings"),
        method="put",
        endpoint="update_device_data/",
        body={"hardware_revision": str(message.payload, "utf-8")},
    )
    if http_status != HTTP_200_OK:
        raise ValueError("Not a 200 status while making start up request: " + str(http_status))


def parse_args():
    main_parser = argparse.ArgumentParser()
    main_parser.add_argument("--daemon", action="store_true", help="Run cloud agent in daemon mode")
    main_parser.add_argument("--provider", help="Provider name to use", default="default")
    subparsers = main_parser.add_subparsers(title="Actions", help="Choose mode:\n", required=False)
    add_provider_parser = subparsers.add_parser("add-provider", help="Add new cloud service provider")
    add_provider_parser.add_argument("provider_name", help="Cloud Provider name to add")
    add_provider_parser.add_argument(
        "base_url", help="Cloud Provider base URL, e.g. https://wirenboard.cloud"
    )
    add_provider_parser.add_argument(
        "agent_url", help="Cloud Provider Agent URL, e.g. https://agent.wirenboard.cloud/api-agent/v1/"
    )
    add_provider_parser.set_defaults(func=add_provider)
    options = main_parser.parse_args()
    return options


def add_provider(options, settings, mqtt):
    if os.path.exists(config_file_path(options.provider_name)):
        print("Provider " + options.provider_name + " already exists")
        return 1
    print("Adding provider " + options.provider_name)
    conf = read_json_file(config_file_path("default"))
    conf["CLOUD_BASE_URL"] = options.base_url
    conf["CLOUD_AGENT_URL"] = options.agent_url
    if not os.path.exists(os.path.join(PROVIDERS_CONF_DIR, options.provider_name)):
        os.makedirs(os.path.join(PROVIDERS_CONF_DIR, options.provider_name))
    with open(config_file_path(options.provider_name), "w") as config_file:
        json.dump(conf, config_file, indent=4)
    if not os.path.exists(f"/var/lib/wb-cloud-agent/providers/{options.provider_name}"):
        os.makedirs(f"/var/lib/wb-cloud-agent/providers/{options.provider_name}")
    start_service(f"wb-cloud-agent@{options.provider_name}.service")
    update_providers_list(settings, mqtt)
    return


def show_activation_link(settings):
    link = read_activation_link(settings)
    if link != "unknown":
        print(f">> {link}")
    else:
        print("No active link. Controller may be already connected")
    return


def run_daemon(mqtt, settings):
    publish_vdev(settings, mqtt)
    with ExitStack() as stack:
        stack.callback(remove_vdev, mqtt)
        while True:
            start = time.perf_counter()
            try:
                make_event_request(settings, mqtt)
            except Exception as ex:
                logging.exception("Error making request to cloud!")
                publish_ctrl(settings, mqtt, "status", "error:" + str(ex))
            else:
                publish_ctrl(settings, mqtt, "status", "ok")
            request_time = time.perf_counter() - start
            logging.debug("Done in: " + str(int(request_time * 1000)) + " ms.")
            time.sleep(settings.REQUEST_PERIOD_SECONDS)


def main():
    options = parse_args()
    cloud_provider = options.provider
    conf_file = config_file_path(cloud_provider)
    settings = AppSettings(provider=cloud_provider, conf_file=conf_file)

    setup_log(settings)

    mqtt = MQTTClient(f"wb-cloud-agent@{cloud_provider}", userdata={"settings": settings})
    mqtt.on_connect = on_connect
    mqtt.on_message = on_message
    mqtt.start()

    if hasattr(options, "func"):
        return options.func(options, settings, mqtt)

    update_providers_list(settings, mqtt)
    make_start_up_request(settings, mqtt)
    send_agent_version(settings)

    if not options.daemon:
        return show_activation_link(settings)

    run_daemon(mqtt, settings)
