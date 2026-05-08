import logging
import platform
from http import HTTPStatus as status
from typing import Callable

from wb.cloud_agent import __version__ as agent_package_version
from wb.cloud_agent import frpc_package_version
from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.handlers.curl import do_curl
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import get_apt_package_version

VersionFieldGetter = Callable[[AppSettings], str]


def get_agent_package_version(_settings: AppSettings) -> str:
    return agent_package_version


def get_frpc_package_version(_settings: AppSettings) -> str:
    return frpc_package_version


def get_python_version(_settings: AppSettings) -> str:
    return platform.python_version()


def get_crypto_engine_key(settings: AppSettings) -> str:
    return settings.client_cert_engine_key


def apt_package_version(package_name: str) -> VersionFieldGetter:
    def getter(_settings: AppSettings) -> str:
        return get_apt_package_version(package_name)

    return getter


PACKAGE_VERSION_FIELDS: dict[str, VersionFieldGetter] = {
    "agent_version": get_agent_package_version,
    "frpc_version": get_frpc_package_version,
    "python_version": get_python_version,
    "mqttrpc_version": apt_package_version("python3-mqttrpc"),
    "paho_mqtt_version": apt_package_version("python3-paho-mqtt"),
    "wb_mqtt_db_version": apt_package_version("wb-mqtt-db"),
    "crypto_engine_key": get_crypto_engine_key,
}


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


def send_packages_version(settings: AppSettings):
    package_versions = collect_package_versions(settings)

    logging.info(
        "Sending package versions: agent=%s, frpc=%s, python=%s, engine_key=%s",
        package_versions["agent_version"],
        package_versions["frpc_version"],
        package_versions["python_version"],
        package_versions["crypto_engine_key"],
    )

    _status_data, http_status = do_curl(
        settings=settings,
        method="put",
        endpoint="update_device_data/",
        params=package_versions,
    )
    if http_status != status.OK:
        logging.error(
            "Not a %s status while making send_packages_version request: %s", status.OK, http_status
        )


def collect_package_versions(settings: AppSettings) -> dict[str, str]:
    """Collect versions useful for cloud-side feature branching."""
    return {field_name: getter(settings) for field_name, getter in PACKAGE_VERSION_FIELDS.items()}


def on_message(userdata: dict, message):
    _status_data, http_status = do_curl(
        userdata.get("settings"),
        method="put",
        endpoint="update_device_data/",
        params={"hardware_revision": str(message.payload, "utf-8")},
    )
    if http_status != status.OK:
        raise ValueError(f"Not a {status.OK} status while making start up request: {http_status}")
