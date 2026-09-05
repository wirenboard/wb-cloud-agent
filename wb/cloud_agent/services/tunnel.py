import logging

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import start_and_enable_service, write_to_file


def update_tunnel_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    write_to_file(fpath=settings.frp_config, contents=payload["config"])
    start_and_enable_service(settings.frp_service, restart=True)
    write_activation_link(settings, UNKNOWN_LINK, mqtt)


def drop_broken_tunnel_config(settings: AppSettings) -> None:
    """Remove an unusable frpc.conf so frpc stops restarting until the cloud sends a new one."""
    config = settings.frp_config
    if not config.is_file():
        return

    try:
        if config.read_text(encoding="utf-8").strip():
            return
        reason = "is empty"
    except (OSError, UnicodeDecodeError) as exc:
        reason = f"cannot be read ({exc})"

    try:
        config.unlink()
    except OSError as exc:
        logging.warning("Tunnel config %s %s but cannot be removed: %s", config, reason, exc)
        return

    logging.warning(
        "Tunnel config %s %s, removed; the tunnel returns with the next cloud event", config, reason
    )
