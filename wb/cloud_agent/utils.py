import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urljoin

from tabulate import tabulate


class ConfigError(Exception):
    """A config file is missing or locally unusable."""


class ConfigReadError(ConfigError):
    """A config file cannot be read without risking data loss."""


@contextmanager
def config_recovery_lock(config_path: Path):
    """Serialize recovery attempts for one provider across agent processes."""
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


if TYPE_CHECKING:
    from wb.cloud_agent.mqtt import MQTTCloudAgent
    from wb.cloud_agent.settings import Provider


@cache
def get_ctrl_serial_number() -> str:
    return subprocess.check_output("wb-gen-serial -s", shell=True).decode().strip()


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def get_controller_url(base_url: str) -> str:
    ctrl_serial_number = get_ctrl_serial_number()
    return urljoin(normalize_base_url(base_url), f"controllers/{ctrl_serial_number}")


def _parse_json_config(config_path: Path) -> dict[str, Any]:
    try:
        data = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError("is missing") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"is not valid JSON ({exc})") from exc
    except OSError as exc:
        raise ConfigReadError(f"cannot be read ({exc})") from exc

    if not data.strip():
        raise ConfigError("is empty")

    try:
        config = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"is not valid JSON ({exc})") from exc

    if not isinstance(config, dict):
        raise ConfigError("is not a JSON object")
    return config


def read_json_config(
    config_path: Path, rebuild: Optional[Callable[[str], dict[str, Any]]] = None
) -> dict[str, Any]:
    try:
        return _parse_json_config(config_path)
    except ConfigError as exc:
        if rebuild is not None:
            if isinstance(exc, ConfigReadError):
                raise
            return rebuild(str(exc))
        if isinstance(exc, ConfigReadError):
            raise
        print(f"Error parsing JSON in: {config_path}")
        sys.exit(6)


def read_plaintext_config(config_path: Path) -> str:
    with config_path.open("r", encoding="utf-8") as f:
        return f.readline().strip()


def write_to_file(fpath: Path, contents: str, create_parent: bool = True, mode: Optional[int] = None) -> None:
    target = fpath.resolve() if fpath.is_symlink() else fpath
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise FileNotFoundError(target.parent)

    try:
        old_stat = target.stat()
    except FileNotFoundError:
        old_stat = None

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            if old_stat is not None:
                os.fchown(temp_file.fileno(), old_stat.st_uid, old_stat.st_gid)
                os.fchmod(temp_file.fileno(), mode if mode is not None else old_stat.st_mode & 0o7777)
            temp_file.write(contents)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(tmp_path, target)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

    dir_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def quarantine_broken_file(fpath: Path) -> Optional[Path]:
    try:
        source_stat = fpath.stat()
        if not fpath.is_file() or source_stat.st_size == 0:
            return None

        fd, tmp_name = tempfile.mkstemp(prefix=f".{fpath.name}.broken-", dir=fpath.parent)
        tmp_path = Path(tmp_name)
        quarantined = fpath.with_name(
            f"{fpath.name}.broken-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-"
            f"{Path(tmp_name).name.rsplit('-', 1)[-1]}"
        )
        try:
            with fpath.open("rb") as source, os.fdopen(fd, "wb") as destination:
                shutil.copyfileobj(source, destination)
                os.fchown(destination.fileno(), source_stat.st_uid, source_stat.st_gid)
                os.fchmod(destination.fileno(), source_stat.st_mode & 0o7777)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(tmp_path, quarantined)
            _fsync_directory(fpath.parent)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        return quarantined
    except OSError as exc:
        logging.warning("Cannot preserve broken file %s: %s", fpath, exc)
        return None


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
