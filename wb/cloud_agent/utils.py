import fcntl
import json
import logging
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from tabulate import tabulate

from wb.cloud_agent.constants import (
    BROKEN_FIRST_SUFFIX,
    BROKEN_LAST_SUFFIX,
    DEFAULT_ENGINE_KEY_PREFIX,
    DEFAULT_FILE_MODE,
    DEVICE_TREE_COMPATIBLE_PATH,
    ENGINE_KEY_PATTERN,
    WB6_DEVICE_TREE_COMPATIBLE,
    WB6_ENGINE_KEY_PREFIX,
)

if TYPE_CHECKING:
    from wb.cloud_agent.mqtt import MQTTCloudAgent
    from wb.cloud_agent.settings import Provider


class ConfigError(Exception):
    """A config file is missing or locally unusable."""


class ConfigReadError(ConfigError):
    """A config file cannot be read without risking data loss."""


@contextmanager
def config_recovery_lock(config_path: Path):
    """
    Serialize recovery attempts for one provider across agent processes.

    A read-only directory or a non-root caller must come out as ConfigError, so that
    the caller reports "not configured" instead of leaking OSError past main().
    """
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise ConfigError(f"cannot be locked for recovery ({exc})") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ConfigError(f"cannot be locked for recovery ({exc})") from exc
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


@cache
def get_ctrl_serial_number() -> str:
    return subprocess.check_output("wb-gen-serial -s", shell=True).decode().strip()


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def get_controller_url(base_url: str) -> str:
    ctrl_serial_number = get_ctrl_serial_number()
    return urljoin(normalize_base_url(base_url), f"controllers/{ctrl_serial_number}")


def read_json_config(config_path: Path) -> dict[str, Any]:
    """Read a JSON config. Raises ConfigError describing what is wrong with it."""
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


def read_plaintext_config(config_path: Path) -> str:
    with config_path.open("r", encoding="utf-8") as f:
        return f.readline().strip()


def resolve_through_symlink(fpath: Path) -> Path:
    """Configs are often symlinks into /mnt/data (wb-configs), so act on the real file."""
    return fpath.resolve() if fpath.is_symlink() else fpath


def stage_file(target: Path, contents: str) -> Path:
    """
    Write contents to a temporary file next to target, ready to be put in place.

    Staging first lets the caller keep the old file until the new one is safely on
    disk, so a failed write cannot leave the directory with no config at all.
    """
    try:
        old_stat = target.stat()
    except FileNotFoundError:
        old_stat = None

    # keep the permissions of an existing file, otherwise behave like a plain
    # open()/write() would with the default umask instead of mkstemp's 0600
    mode = old_stat.st_mode & 0o7777 if old_stat is not None else DEFAULT_FILE_MODE

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            if old_stat is not None:
                os.fchown(temp_file.fileno(), old_stat.st_uid, old_stat.st_gid)
            os.fchmod(temp_file.fileno(), mode)
            temp_file.write(contents)
            temp_file.flush()
            os.fsync(temp_file.fileno())
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def commit_staged(staged: Path, target: Path) -> None:
    """Put a staged file in place atomically."""
    try:
        os.replace(staged, target)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    _fsync_directory(target.parent)


def write_to_file(fpath: Path, contents: str, create_parent: bool = True) -> None:
    target = resolve_through_symlink(fpath)
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise FileNotFoundError(target.parent)

    commit_staged(stage_file(target, contents), target)


def quarantine_broken_file(fpath: Path) -> Path:
    """
    Move a damaged file aside, keeping its content untouched.

    Two fixed slots, "first" and "last": the first one holds whatever the operator had
    before the very first corruption, the last one describes the most recent failure.
    Slots are used instead of timestamps on purpose - these controllers boot with a
    wrong clock until NTP catches up, which is exactly the situation this code runs in,
    and sorting broken copies by name would then drop the wrong one.

    Neither hardlinking nor renaming needs read access to the file itself, only a writable
    parent directory, so a config that cannot be read is preserved rather than lost. A
    hardlink is tried first because it leaves the original in place until the replacement
    is committed; filesystems without hardlinks fall back to a rename. Raises OSError if
    the file cannot be set aside; the caller then leaves it alone.
    """
    target = resolve_through_symlink(fpath)
    first = target.with_name(f"{target.name}{BROKEN_FIRST_SUFFIX}")
    quarantined = first if not first.exists() else target.with_name(f"{target.name}{BROKEN_LAST_SUFFIX}")

    quarantined.unlink(missing_ok=True)
    try:
        os.link(target, quarantined)
    except OSError:
        os.replace(target, quarantined)
    _drop_legacy_quarantine_copies(target)
    _fsync_directory(target.parent)
    return quarantined


def _drop_legacy_quarantine_copies(fpath: Path) -> None:
    """Earlier versions named copies after the time of the failure; keep /etc tidy."""
    slots = {f"{fpath.name}{BROKEN_FIRST_SUFFIX}", f"{fpath.name}{BROKEN_LAST_SUFFIX}"}
    for stale in fpath.parent.glob(f"{fpath.name}.broken-*"):
        if stale.name in slots:
            continue
        try:
            stale.unlink()
        except OSError as exc:
            logging.warning("Cannot remove stale broken copy %s: %s", stale, exc)


def _fsync_directory(directory: Path) -> None:
    """Best effort: the rename is already committed, a failure here must not undo it."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        logging.warning("Cannot fsync directory %s: %s", directory, exc)
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        logging.warning("Cannot fsync directory %s: %s", directory, exc)
    finally:
        os.close(dir_fd)


def local_engine_key_prefix() -> str:
    """ATECC engine key prefix for this controller: the chip sits on a different I2C bus on WB6."""
    try:
        compatible = Path(DEVICE_TREE_COMPATIBLE_PATH).read_bytes().split(b"\0")
    except OSError:
        return DEFAULT_ENGINE_KEY_PREFIX
    if WB6_DEVICE_TREE_COMPATIBLE.encode() in compatible:
        return WB6_ENGINE_KEY_PREFIX
    return DEFAULT_ENGINE_KEY_PREFIX


def local_engine_key(value: Any) -> Any:
    """Point an engine key at the I2C bus the ATECC chip really sits on."""
    if not isinstance(value, str):
        return value
    return re.sub(ENGINE_KEY_PATTERN, local_engine_key_prefix(), value)


def with_local_engine_key(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the config with the engine key pointed at this board's bus."""
    if "CLIENT_CERT_ENGINE_KEY" not in config:
        return config
    return {**config, "CLIENT_CERT_ENGINE_KEY": local_engine_key(config["CLIENT_CERT_ENGINE_KEY"])}


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


def stop_service(service: str, timeout: int = 120) -> None:
    logging.debug("Stopping service %s", service)
    subprocess.run(["systemctl", "stop", service], check=True, timeout=timeout)


def disable_service(service: str, timeout: int = 120) -> None:
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


def stop_and_disable_service(service: str, timeout: int = 120) -> None:
    stop_service(service, timeout=timeout)
    disable_service(service, timeout=timeout)


def try_stop_and_disable_service(service: str, timeout: int = 120) -> None:
    """Best-effort variant: each step runs even if the other one fails."""
    try:
        stop_service(service, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logging.warning("Cannot stop service %s: %s", service, exc)
    try:
        disable_service(service, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logging.warning("Cannot disable service %s: %s", service, exc)


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
