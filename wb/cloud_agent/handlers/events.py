import logging
from http import HTTPStatus as status

from wb.cloud_agent.constants import UNBIND_CTRL_REQUEST_TIMEOUT
from wb.cloud_agent.handlers.curl import do_curl
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import update_activation_link
from wb.cloud_agent.services.diagnostics import fetch_diagnostics
from wb.cloud_agent.services.metrics import update_metrics_config
from wb.cloud_agent.services.tunnel import update_tunnel_config
from wb.cloud_agent.settings import AppSettings

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
        raise ValueError(f"Not a {status.OK} status while retrieving event: {http_status}")

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

    event_confirm(settings, event_id)


def event_confirm(settings: AppSettings, event_id: str) -> None:
    _event_data, http_status = do_curl(
        settings=settings, method="post", endpoint="events/" + event_id + "/confirm/"
    )
    if http_status != status.NO_CONTENT:
        raise ValueError(f"Not a {status.NO_CONTENT} status on event confirmation: {http_status}")


def event_delete_controller(settings: AppSettings) -> int:
    retry_opts = (
        "--connect-timeout",
        str(UNBIND_CTRL_REQUEST_TIMEOUT - 1),
        "--retry",
        "0",
        "--max-time",
        str(UNBIND_CTRL_REQUEST_TIMEOUT),
    )
    try:
        _event_data, http_status = do_curl(
            settings=settings, method="delete", endpoint="delete-controller/", retry_opts=retry_opts
        )
    except Exception as exc:  # pylint: disable=W0718
        logging.warning(
            "Warning: The controller on the remote server could not be detached due to network problems.\n"
            "Unbind it manually using the command: 'wb-cloud-agent cloud-unbind %s'",
            settings.cloud_base_url,
        )
        logging.debug("Error while sending delete-controller event: %s", exc)
        return 1

    if http_status != status.NO_CONTENT:
        logging.error(
            "Not a %s status while making event_delete_controller request: %s", status.NO_CONTENT, http_status
        )
        return 1

    logging.info("Controller has been successfully detached from: %s", settings.cloud_base_url)
    return 0
