import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import (
    is_port_taken,
    reset_failed_service,
    start_and_enable_service,
    stop_service,
    write_to_file,
)

FRPC_ADMIN_ADDR = "127.0.0.1"
FRPC_ADMIN_PORTS = range(7106, 7116)
FRPC_CONFIG_MODE = 0o600

COMMON_HEADER = re.compile(r"^\[common\][ \t]*\n", re.MULTILINE)
TUNNEL_TOKEN = re.compile(r"^meta_tunnel_token[ \t]*=[ \t]*(\S+)", re.MULTILINE)
ADMIN_PORT = re.compile(r"^admin_port[ \t]*=[ \t]*(\d+)", re.MULTILINE)


def update_tunnel_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    """Облако просит restart, когда меняется [common] или туннели надо поднять заново; без флага
    (старое облако) тоже перезапускаем. Иначе применяем конфиг через admin API frpc — живые сессии целы."""
    config = payload["config"]
    port = _admin_port(settings.frp_config)
    if payload.get("restart", True) or port is None or not _reload_frpc(settings, config, port):
        _restart_frpc(settings, config, port)
    write_activation_link(settings, UNKNOWN_LINK, mqtt)


def _reload_frpc(settings: AppSettings, config: str, port: int) -> bool:
    write_to_file(settings.frp_config, _with_admin_api(config, port), FRPC_CONFIG_MODE)
    return reload_frpc(settings.frp_config)


def _restart_frpc(settings: AppSettings, config: str, previous_port: Optional[int]) -> None:
    """frpc с занятым admin-портом не стартует и крутится в рестарт-цикле systemd, поэтому порт выбираем
    после остановки своего frpc: прежний, иначе первый свободный (свой у каждого провайдера)."""
    stop_service(settings.frp_service)
    reset_failed_service(settings.frp_service)
    port = _free_admin_port(previous_port)
    if port is None:
        logging.warning("No free port in %s, frpc runs without admin API", FRPC_ADMIN_PORTS)
    else:
        config = _with_admin_api(config, port)
    write_to_file(settings.frp_config, config, FRPC_CONFIG_MODE)
    start_and_enable_service(settings.frp_service)


def _admin_port(config_path: Path) -> Optional[int]:
    """Порт admin API работающего frpc — из прошлого конфига, который писал этот же агент."""
    try:
        match = ADMIN_PORT.search(config_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return int(match.group(1)) if match else None


def _free_admin_port(preferred: Optional[int]) -> Optional[int]:
    candidates = [preferred] if preferred in FRPC_ADMIN_PORTS else []
    for port in candidates + [port for port in FRPC_ADMIN_PORTS if port != preferred]:
        if not is_port_taken(FRPC_ADMIN_ADDR, port):
            return port
    return None


def _with_admin_api(config: str, port: int) -> str:
    """Пароль — токен connection: он меняется только вместе с restart, так что у reload он актуален."""
    token = TUNNEL_TOKEN.search(config)
    if token is None:
        return config
    admin = (
        f"admin_addr = {FRPC_ADMIN_ADDR}\nadmin_port = {port}\n"
        f"admin_user = wb-cloud-agent\nadmin_pwd = {token.group(1)}\n"
    )
    return COMMON_HEADER.sub(lambda header: header.group(0) + admin, config, count=1)


def reload_frpc(config_path: Path, timeout: int = 60) -> bool:
    """Перечитать прокси-секции работающего frpc через его admin API (адрес и учётка — из конфига).
    Неизменённые туннели и их сессии остаются. False — reload не прошёл, нужен restart."""
    try:
        result = subprocess.run(
            ["frpc", "reload", "-c", str(config_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logging.warning("frpc reload failed: %s", exc)
        return False

    if result.returncode == 0:
        return True

    logging.warning(
        "frpc reload failed (exit %s): %s", result.returncode, (result.stdout + result.stderr).strip()
    )
    return False
