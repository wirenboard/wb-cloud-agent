import logging
from http import HTTPStatus as status

from wb.cloud_agent import __version__ as package_version
from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.handlers.curl import do_curl
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.settings import AppSettings


def make_start_up_request(settings: AppSettings, mqtt: MQTTCloudAgent):
    status_data, http_status = do_curl(settings=settings, method="get", endpoint="agent-start-up/")
    if http_status != status.OK:
        logging.debug("http_status=%s status_data=%s", http_status, status_data)
        raise ValueError(f"Not a {status.OK} status while making start up request: {http_status}")

    if "activated" not in status_data or "activationLink" not in status_data:
        raise ValueError(f"Invalid response data while making start up request: {status_data}")

    activated = status_data["activated"]
    activation_link = status_data["activationLink"]

    if activated or not activation_link:
        write_activation_link(settings, UNKNOWN_LINK, mqtt)
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
        logging.error("Not a %s status while making send_agent_version request: %s", status.OK, http_status)


def on_message(userdata: dict, message):
    _status_data, http_status = do_curl(
        userdata.get("settings"),
        method="put",
        endpoint="update_device_data/",
        params={"hardware_revision": str(message.payload, "utf-8")},
    )
    if http_status != status.OK:
        raise ValueError(f"Not a {status.OK} status while making start up request: {http_status}")
