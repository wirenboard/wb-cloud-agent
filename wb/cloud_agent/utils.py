import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional
from urllib.parse import urljoin

from tabulate import tabulate

if TYPE_CHECKING:
    from wb.cloud_agent.mqtt import MQTTCloudAgent
    from wb.cloud_agent.settings import Provider


class ConfigError(Exception):
    """A config file is missing, empty, unreadable or not a JSON object."""


@cache
def get_ctrl_serial_number() -> str:
    return subprocess.check_output("wb-gen-serial -s", shell=True).decode().strip()


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def get_controller_url(base_url: str) -> str:
    ctrl_serial_number = get_ctrl_serial_number()
    return urljoin(normalize_base_url(base_url), f"controllers/{ctrl_serial_number}")


def _parse_json_config(config_path: Path) -> dict[str, str]:
    """Return the config object, or raise ConfigError describing why the file is unusable."""
    try:
        data = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError("is missing") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot be read ({exc})") from exc

    if not data.strip():
        raise ConfigError("is empty")

    try:
        conf = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"is not valid JSON ({exc})") from exc

    if not isinstance(conf, dict):
        raise ConfigError("is not a JSON object")
    return conf


def read_json_config(config_path: Path, rebuild: Optional[Callable[[str], dict]] = None) -> dict[str, str]:
    """Parse a JSON config, delegating to rebuild(reason) when the file cannot be used."""
    try:
        return _parse_json_config(config_path)
    except ConfigError as exc:
        if rebuild is None:
            raise
        return rebuild(str(exc))


def read_plaintext_config(config_path: Path) -> str:
    """Return the first line, or an empty string when the file is missing or unreadable."""
    try:
        with config_path.open("r", encoding="utf-8") as f:
            return f.readline().strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        logging.warning("Cannot read %s: %s, treating the value as unknown", config_path, exc)
        return ""


def write_to_file(fpath: Path, contents: str) -> None:
    """Write atomically: a power cut leaves either the old file or the new one, never a truncated one."""
    fpath.parent.mkdir(parents=True, exist_ok=True)
    # Same directory, pid-suffixed: concurrent provider instances cannot collide on it.
    tmp_path = fpath.with_name(f".{fpath.name}.tmp.{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, fpath)
    finally:
        tmp_path.unlink(missing_ok=True)

    dir_fd = os.open(fpath.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def quarantine_broken_file(fpath: Path) -> Optional[Path]:
    """Move a broken file aside before it is overwritten; empty files are dropped, they preserve nothing."""
    try:
        if not fpath.is_file() or fpath.stat().st_size == 0:
            return None
        quarantined = fpath.with_name(f"{fpath.name}.broken-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        os.replace(fpath, quarantined)
        return quarantined
    except OSError as exc:
        logging.warning("Cannot preserve broken file %s: %s", fpath, exc)
        return None


def start_and_enable_service(service: str, restart: bool = False, timeout: int = 120) -> None:
    logging.debug("Enabling service %s", service)

    result = subprocess.run(
        ["systemctl", "enable", service],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stdout:
        logging.debug("Enabling service stdout: %s", result.stdout.strip())
    if result.stderr:
        logging.debug("Enabling service stderr: %s", result.stderr.strip())

    if restart:
        logging.debug("Restarting service %s", service)
        subprocess.run(["systemctl", "restart", service], check=True, timeout=timeout)
    else:
        logging.debug("Starting service %s", service)
        subprocess.run(["systemctl", "start", service], check=True, timeout=timeout)


def stop_and_disable_service(service: str, timeout: int = 120) -> None:
    logging.debug("Disabling service %s", service)
    result = subprocess.run(
        ["systemctl", "disable", service],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if result.stdout:
        logging.debug("Disabling service stdout: %s", result.stdout.strip())
    if result.stderr:
        logging.debug("Disabling service stderr: %s", result.stderr.strip())

    logging.debug("Stopping service %s", service)
    subprocess.run(["systemctl", "stop", service], check=True, timeout=timeout)


def show_providers_table(providers: list["Provider"]) -> None:
    if not providers:
        print("No one provider was found")
        return

    table = [[p.name, p.display_url] for p in providers]
    headers = ["Provider", "Controller Url / Activation Url"]
    print(tabulate(table, headers=headers, tablefmt="github"))


def parse_headers(header_section: str) -> dict[str, str]:
    headers = {}
    for line in header_section.splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip()] = value.strip()
    return headers


def get_apt_package_version(package_name: str) -> str:
    """Get version of installed APT package using dpkg-query."""
    try:
        result = subprocess.run(
            ["dpkg-query", "--showformat=${Version}", "--show", package_name],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def handle_connection_state(prev_value: bool, new_value: bool, msg: str, mqtt: "MQTTCloudAgent") -> bool:
    if prev_value != new_value:
        logging.info(msg)

    mqtt.publish_ctrl("status", "ok" if new_value else msg)
    return new_value
