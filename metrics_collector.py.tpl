#!/usr/bin/env python3
"""Wiren Board Cloud metrics collector — runs on Wiren Board controllers.

Each ``INTERVAL_SECONDS`` the script reads new entries from ``wb-mqtt-db`` via MQTT-RPC
(uid-based pagination, ver=0), adds static retained topics read directly from the MQTT
broker (see STATIC_RETAINED_TOPICS), and sends everything to the cloud as gzip-compressed
Influx Line Protocol over curl with mTLS (ATECC hardware key + agent certificate).

Progress is saved to STATE_FILE after each successfully delivered sub-batch. On first start
(no state file) ``find_start_uid`` looks back INITIAL_UID_LOOKBEHIND_SECONDS to skip the
full historical backlog. ``collect_once`` returns ``True`` when there is more backlog to drain.

Compatibility: runs on controllers. Requires Python 3.9+, ``python3-wb-common``, ``python3-mqttrpc``,
and the ``curl`` binary (with ATECC hardware key support).

ILP row format::

    wb_mqtt,topic=/devices/metrics/controls/cpu_usage value="17.3" 1712345678000000000
"""

from __future__ import annotations

import argparse
import functools
import gzip
import logging
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    from mqttrpc.client import (  # type: ignore[import-not-found]
        TimeoutError as MQTTRPCTimeoutError,
        TMQTTRPCClient,
    )
    from wb_common.mqtt_client import MQTTClient  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - dependencies are installed on controllers
    MQTTClient = None
    TMQTTRPCClient = None
    # Fallback so `except MQTTRPCTimeoutError` stays valid when the dependency is absent
    # (only happens in unit tests; on controllers the real mqttrpc TimeoutError is used).
    MQTTRPCTimeoutError = TimeoutError


# ── Identity ──────────────────────────────────────────────────────────────────────────────
VERSION = "1.0.12"
CREATED_AT = "$created_at"

EXIT_FAILURE = 1
EXIT_INVALID_ARGUMENT = 2
EXIT_INVALID_CONFIG = 6
EXIT_STOPPED = 7
MQTT_AUTH_ERROR_CODES = (4, 5, 134, 135)

_STOP_REQUESTED = threading.Event()

# ── Connection / authentication ───────────────────────────────────────────────────────────
BROKER_URL = "$mqtt_broker_url"
CLIENT_ID = "$mqtt_client_id"
METRICS_URL = "$metrics_url"
CRYPTO_ENGINE_KEY = "$crypto_engine_key"
CRYPTO_ENGINE = "$crypto_engine"
CERT_FILE = "/var/lib/wb-cloud-agent/device_bundle.crt.pem"
STATE_FILE = "$state_file"

# ── Data schema ───────────────────────────────────────────────────────────────────────────
MEASUREMENT = "$measurement_name"

# ── Polling ───────────────────────────────────────────────────────────────────────────────
INTERVAL_SECONDS = int("$interval_seconds")
RPC_TIMEOUT_SECONDS = int("$rpc_timeout_seconds")
GET_CHANNELS_RPC_TIMEOUT_SECONDS = int("$get_channels_rpc_timeout_seconds")
MAX_RECORDS = int("$max_records")
CHANNEL_BATCH_SIZE = int("$channel_batch_size")
CHANNELS_REFRESH_INTERVAL_SECONDS = int("$channels_refresh_interval_seconds")
# Pause between consecutive get_values batches in catch-up mode — gives the wb-mqtt-db
# writer a chance to drain its commit queue before we issue the next heavy read.
CATCH_UP_BATCH_SLEEP_SECONDS = float("$catch_up_batch_sleep_seconds")
# How often to re-read static MQTT retained topics and write them to TimescaleDB.
# Several static topics (dev_root_total_space, data_total_space) are used in
# Grafana dashboard calculations: locf() with treat_null_as_missing=true fills
# gaps between writes, so this interval can be set independently of INTERVAL_SECONDS.
STATIC_REFRESH_INTERVAL = int("$static_refresh_interval")

# ── Catch-up (backlog replay after downtime) ──────────────────────────────────────────────
CATCH_UP_MAX_RECORDS = int("$catch_up_max_records")
# Derived from Traefik rate limit: period_s // average + 1; pre-computed on Backend when rendering.
CATCH_UP_SLEEP_SECONDS = int("$catch_up_sleep_seconds")
# On first start (no state file), skip records older than this many seconds to avoid replaying
# the full historical backlog. Two intervals gives enough overlap to not lose recent data.
INITIAL_UID_LOOKBEHIND_SECONDS = INTERVAL_SECONDS * 2

# ── wb-mqtt-db service liveness check ─────────────────────────────────────────────────────
WB_MQTT_DB_SERVICE_NAME = "wb-mqtt-db.service"
SERVICE_STATE_CHECK_TIMEOUT_SECONDS = 5

# ── HTTP send ─────────────────────────────────────────────────────────────────────────────
CURL_CONNECT_TIMEOUT_SECONDS = int("$curl_connect_timeout_seconds")
CURL_MAX_TIME_SECONDS = int("$curl_max_time_seconds")
SEND_BATCH_SIZE = int("$send_batch_size")  # max ILP lines per curl request; keep <= MAX_RECORDS
MAX_REQUEST_BYTES = int("$max_request_bytes")  # max compressed body; pre-flight checked before curl
SEND_MAX_RETRIES = int("$send_max_retries")  # retry attempts on HTTP 429 rate-limit responses
SEND_RATE_LIMIT_RETRY_DELAY_SECONDS = int("$send_rate_limit_retry_delay_seconds")

# ── Static MQTT topics (timestamps always forced to now) ──────────────────────────────────
# Published only once at service/boot startup; never re-sent by wb-rules-system or
# wb-mqtt-metrics. All WirenBoard MQTT controls use retain=True, but only these "static"
# controls accumulate stale timestamps during long controller uptime. wb-mqtt-db always
# sets the ``retain`` field to False (known limitation), so topic matching is the only
# reliable way to detect them.
STATIC_RETAINED_TOPICS: frozenset[str] = frozenset("$static_retained_topics".split("|"))  # noqa: SIM905

_static_last_refresh: float = 0.0
_channels_cache: list[dict[str, Any]] = []
_channels_last_refresh: float = 0.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s wb-cloud-metrics: %(message)s",
)
logger = logging.getLogger(__name__)


class CollectorExit(RuntimeError):
    """Request a controlled collector exit with a systemd-facing status code."""

    def __init__(self, exit_code: int):
        super().__init__(exit_code)
        self.exit_code = exit_code


class MQTTConnectionState:
    """Share MQTT connection results between Paho callbacks and the main thread."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._connected = False
        self._fatal_exit_code: int | None = None
        self._connect_failure_reported = False
        self._connection_generation = 0

    def on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, *_args: Any) -> None:
        """Record a successful CONNACK or map a broker rejection to an exit code."""
        code = int(getattr(reason_code, "value", reason_code))
        with self._condition:
            if code == 0:
                self._connected = True
                self._connect_failure_reported = False
                self._connection_generation += 1
            else:
                self._connected = False
                self._fatal_exit_code = (
                    EXIT_INVALID_ARGUMENT if code in MQTT_AUTH_ERROR_CODES else EXIT_FAILURE
                )
                logger.error("MQTT broker rejected connection (CONNACK code %s)", code)
            self._condition.notify_all()

    def on_connect_fail(self, _client: Any, _userdata: Any) -> None:
        """Report an unavailable broker once."""
        with self._condition:
            if not self._connect_failure_reported:
                logger.warning("MQTT broker is unavailable, waiting for it")
                self._connect_failure_reported = True

    def on_disconnect(self, _client: Any, _userdata: Any, _reason_code: Any, *_args: Any) -> None:
        """Record connection loss; Paho will keep reconnecting in the same process."""
        with self._condition:
            self._connected = False
            self._condition.notify_all()

    def wait_for_connection(self) -> int | None:
        """Wait for CONNACK, a fatal rejection, or a service stop request."""
        with self._condition:
            while not self._connected and self._fatal_exit_code is None:
                if _STOP_REQUESTED.is_set():
                    return EXIT_STOPPED
                self._condition.wait(0.1)
            return self._fatal_exit_code

    def wait_for_exit(self, timeout: float) -> int | None:
        """Wait up to timeout for a fatal CONNACK or a service stop request."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._fatal_exit_code is None:
                if _STOP_REQUESTED.is_set():
                    return EXIT_STOPPED
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, 0.1))
            return self._fatal_exit_code

    def requested_exit_code(self) -> int | None:
        """Return the currently requested controlled exit, if any."""
        with self._condition:
            if self._fatal_exit_code is not None:
                return self._fatal_exit_code
        return EXIT_STOPPED if _STOP_REQUESTED.is_set() else None

    def connection_generation(self) -> int:
        """Return the count of successful broker connections for this client."""
        with self._condition:
            return self._connection_generation


# Characters that must be escaped in ILP tag values and field string values.
# MEASUREMENT maps directly to a PostgreSQL table name, so it cannot contain
# spaces, commas or backslashes — no escaping needed there.
# See: https://docs.influxdata.com/influxdb/v1/write_protocols/line_protocol_tutorial/#special-characters
_RE_ESCAPE_TAG = re.compile("[\\\\, =]")  # backslash, space, comma, equals
_RE_ESCAPE_FIELD_STR = re.compile('[\\\\"]')  # backslash, double-quote


def _escape_char(m: re.Match) -> str:
    return "\\" + m.group()


def escape_tag(value: object) -> str:
    """Escape Influx Line Protocol tag value.

    Applied to the MQTT topic used as the ``topic`` tag.
    Example: ``'/devices/system/controls/Release name'``
          → ``'/devices/system/controls/Release\\ name'``
    """
    return _RE_ESCAPE_TAG.sub(_escape_char, str(value))


def escape_field_string(value: object) -> str:
    """Escape string field value for Influx Line Protocol.

    Applied to string MQTT control values before quoting them in the ILP line.
    Example: ``'wb-2024.05 "bullseye"'`` → ``'wb-2024.05 \\"bullseye\\"'``
    """
    return _RE_ESCAPE_FIELD_STR.sub(_escape_char, str(value))


def split_channel(channel_name: object) -> Any:
    """Convert wb-mqtt-db channel name to `[device, control]` pair."""
    normalized = str(channel_name).strip("/")
    if normalized.startswith("devices/"):
        parts = normalized.split("/")
        if len(parts) >= 4 and parts[2] == "controls":
            return [parts[1], "/".join(parts[3:])]
    if "/" not in normalized:
        return None
    device, control = normalized.split("/", 1)
    if not device or not control:
        return None
    return [device, control]


def topic_from_pair(pair: list[str]) -> str:
    """Build canonical full MQTT topic from `[device, control]` pair.

    The full form ``/devices/<device>/controls/<control>`` matches what users see in
    homeUI and ``mosquitto_sub``, so this is what we publish to TimescaleDB and
    show in Grafana queries.
    """
    return f"/devices/{pair[0]}/controls/{pair[1]}"


def normalize_channel(channel_name: object) -> Any:
    """Normalize one wb-mqtt-db channel to collector channel metadata."""
    pair = split_channel(channel_name)
    if not pair:
        return None
    return {"pair": pair, "topic": topic_from_pair(pair)}


def normalize_channels(channels_response: Any) -> list[dict[str, Any]]:
    """Normalize all channels returned by wb-mqtt-db history RPC."""
    if isinstance(channels_response, dict):
        channel_names = list(channels_response.keys())
    else:
        channel_names = list(channels_response or [])

    channels = []
    for channel_name in channel_names:
        channel = normalize_channel(channel_name)
        if channel:
            channels.append(channel)
    return channels


def unwrap_rpc_result(result: Any) -> dict[str, Any]:
    """Support both raw and JSON-RPC-like response wrappers from MQTT-RPC."""
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return result["result"]
    return result or {}


def load_last_uid() -> int:
    """Read last successfully delivered wb-mqtt-db UID from the state file."""
    path = Path(STATE_FILE)
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read state file %s: %s", path, exc)
        return 0


def save_last_uid(uid: int) -> None:
    """Atomically store last successfully delivered wb-mqtt-db UID."""
    path = Path(STATE_FILE)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(str(int(uid)), encoding="utf-8")
    tmp_path.replace(path)


def _get_channel_batches(
    channels: list[dict[str, Any]], batch_size: int | None = None
) -> list[list[dict[str, Any]]]:
    """Split channels into chunks small enough for stable wb-mqtt-db RPC calls."""
    batch_size = max(1, batch_size or CHANNEL_BATCH_SIZE)
    return [channels[start : start + batch_size] for start in range(0, len(channels), batch_size)]


def log_duration(operation: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: log at INFO how long the wrapped call took, even when it raises.

    Avoids sprinkling ``time.perf_counter()`` around every wb-mqtt-db RPC / HTTP send and
    surfaces the duration in the journal (e.g. a get_channels that finally answered in 41s).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                logger.info("%s: %.2fs", operation, time.perf_counter() - start)

        return wrapper

    return decorator


def _call_history_get_values(rpc: Any, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return unwrap_rpc_result(
            rpc.call(
                driver="db_logger",
                service="history",
                method="get_values",
                params=params,
                timeout=RPC_TIMEOUT_SECONDS + 5,
            )
        )
    except MQTTRPCTimeoutError:
        logger.error(
            "wb-mqtt-db get_values RPC timed out after %ds (channels=%d, limit=%s)",
            RPC_TIMEOUT_SECONDS + 5,
            len(params.get("channels", []) or []),
            params.get("limit"),
        )
        raise


def find_start_uid(rpc: Any, channels: list[dict[str, Any]]) -> int:
    """On first start, find the uid to begin from so we skip the full historical backlog.

    Queries wb-mqtt-db for the oldest record within INITIAL_UID_LOOKBEHIND_SECONDS and
    returns uid - 1 so the main loop includes that record. Returns 0 if no recent data
    exists — the main loop will then scan from the very beginning.
    """
    values = []
    for channel_batch in _get_channel_batches(channels):
        params = {
            "ver": 0,
            "channels": [channel["pair"] for channel in channel_batch],
            "timestamp": {"gt": time.time() - INITIAL_UID_LOOKBEHIND_SECONDS},
            "limit": 1,
            "request_timeout": RPC_TIMEOUT_SECONDS,
        }
        values.extend(_call_history_get_values(rpc, params).get("values", []))

    if values:
        uid = min(value_uid(value) for value in values)
        start_uid = max(0, uid - 1)
        logger.info(
            "First start: initialising from uid=%s (lookbehind %ds)",
            start_uid,
            INITIAL_UID_LOOKBEHIND_SECONDS,
        )
        return start_uid
    logger.info("First start: no recent data found, scanning from the beginning")
    return 0


def connect_mqtt() -> tuple[Any, MQTTConnectionState]:
    """Connect to local MQTT broker used by wb-mqtt-db RPC."""
    if MQTTClient is None:
        raise RuntimeError("python3-wb-common is not installed")

    logger.info("Connecting to MQTT broker %s (client_id_prefix=%s)", BROKER_URL, CLIENT_ID)
    state = MQTTConnectionState()
    client = MQTTClient(CLIENT_ID, BROKER_URL)
    client.on_connect = state.on_connect
    client.on_connect_fail = state.on_connect_fail
    client.on_disconnect = state.on_disconnect
    try:
        client.start()
        exit_code = state.wait_for_connection()
    except Exception:
        stop_mqtt_client(client)
        raise
    if exit_code is not None:
        stop_mqtt_client(client, report_unavailable=exit_code == EXIT_STOPPED)
        raise CollectorExit(exit_code)
    logger.info("MQTT broker connected")
    return client, state


def create_rpc_client(mqtt_client: Any) -> Any:
    """Create MQTT-RPC client for wb-mqtt-db history service."""
    if TMQTTRPCClient is None:
        raise RuntimeError("python3-mqttrpc is not installed")

    rpc = TMQTTRPCClient(mqtt_client)
    mqtt_client.on_message = rpc.on_mqtt_message
    return rpc


def refresh_rpc_client_after_reconnect(
    mqtt_client: Any,
    rpc: Any,
    mqtt_state: MQTTConnectionState,
    rpc_connection_generation: int,
) -> tuple[Any, int]:
    """Recreate the RPC client after Paho reconnects and loses its reply subscriptions."""
    current_generation = mqtt_state.connection_generation()
    if current_generation == rpc_connection_generation:
        return rpc, rpc_connection_generation

    logger.info("MQTT broker connection restored; recreating MQTT-RPC client subscriptions")
    return create_rpc_client(mqtt_client), current_generation


def connect_mqtt_rpc() -> tuple[Any, Any, MQTTConnectionState]:
    """Connect to MQTT broker and create a matching MQTT-RPC client."""
    mqtt_client, state = connect_mqtt()
    return mqtt_client, create_rpc_client(mqtt_client), state


def stop_mqtt_client(mqtt_client: Any, report_unavailable: bool = False) -> None:
    """Stop MQTT client and keep shutdown/reconnect cleanup best-effort."""
    if report_unavailable:
        try:
            if not mqtt_client.is_connected():
                logger.error("MQTT broker is unavailable during shutdown")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Cannot determine MQTT connection state during shutdown: %s", exc)
    try:
        mqtt_client.stop()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Cannot stop MQTT client: %s", exc)


def reconnect_mqtt_rpc(mqtt_client: Any) -> tuple[Any, Any, MQTTConnectionState]:
    """Recreate MQTT and MQTT-RPC clients after a failed collector iteration."""
    stop_mqtt_client(mqtt_client)
    mqtt_client, rpc, state = connect_mqtt_rpc()
    logger.info("MQTT/RPC client reconnected after metrics iteration failure")
    return mqtt_client, rpc, state


@log_duration("wb-mqtt-db get_channels RPC")
def get_channels(rpc: Any) -> list[dict[str, Any]]:
    """Get known wb-mqtt-db channels and normalize them to device/control pairs."""
    logger.info("wb-mqtt-db get_channels RPC call (last seen %d channels)", len(_channels_cache))
    try:
        result = unwrap_rpc_result(
            rpc.call(
                driver="db_logger",
                service="history",
                method="get_channels",
                params={},
                timeout=GET_CHANNELS_RPC_TIMEOUT_SECONDS,
            )
        )
    except MQTTRPCTimeoutError:
        logger.error("wb-mqtt-db get_channels RPC timed out after %ds", GET_CHANNELS_RPC_TIMEOUT_SECONDS)
        raise
    raw_channels = result.get("channels", {})
    channels = normalize_channels(raw_channels)
    logger.info("Discovered %d metric channels", len(channels))
    return channels


def get_cached_channels(rpc: Any) -> list[dict[str, Any]]:
    """Return cached wb-mqtt-db channels and refresh them periodically."""
    global _channels_cache, _channels_last_refresh

    now = time.time()
    if _channels_cache and now - _channels_last_refresh < CHANNELS_REFRESH_INTERVAL_SECONDS:
        return _channels_cache

    try:
        channels = get_channels(rpc)
    except Exception as exc:  # pylint: disable=broad-except
        if _channels_cache:
            logger.warning(
                "Cannot refresh metric channels: %s; using cached %d channels",
                exc,
                len(_channels_cache),
            )
            return _channels_cache
        raise

    if channels:
        _channels_cache = channels
        _channels_last_refresh = now
    return channels


def get_static_values_from_mqtt(mqtt_client: Any) -> list[dict[str, Any]]:
    """Read static retained values directly from MQTT broker.

    Unlike wb-mqtt-db, which evicts old records when the row-count limit (values_total)
    is reached, the broker retains the last published value indefinitely. Static topics
    are published once at controller boot and never re-sent, so retained messages in the
    broker are the only reliable source once wb-mqtt-db has purged those records.
    """
    received: dict[str, str] = {}

    def _on_static_message(_client: Any, _userdata: Any, message: Any) -> None:
        topic = message.topic
        if topic in STATIC_RETAINED_TOPICS:
            received[topic] = message.payload.decode("utf-8", errors="replace")

    topics = sorted(STATIC_RETAINED_TOPICS)
    for topic in topics:
        mqtt_client.message_callback_add(topic, _on_static_message)
    mqtt_client.subscribe([(topic, 0) for topic in topics])

    # Retained messages are delivered immediately after subscribe on a local broker;
    # 0.5 s is enough for the async network thread to process all incoming messages.
    _sleep(0.5)

    for topic in topics:
        mqtt_client.message_callback_remove(topic)
    mqtt_client.unsubscribe(topics)

    results = []
    for topic, payload in received.items():
        pair = split_channel(topic)
        if pair and payload:
            results.append(
                {
                    "device": pair[0],
                    "control": pair[1],
                    "value": payload,
                    "timestamp": time.time(),
                }
            )

    logger.info(
        "Fetched %d/%d static values from MQTT retained",
        len(results),
        len(topics),
    )
    return results


@log_duration("wb-mqtt-db get_values RPC")
def get_values(
    rpc: Any,
    channels: list[dict[str, Any]],
    last_uid: int,
    max_records: int,
    inter_batch_sleep: float = 0.0,
) -> dict[str, Any]:
    """Read metric values newer than last_uid from wb-mqtt-db."""
    channel_batches = _get_channel_batches(channels)
    values: list[dict[str, Any]] = []
    has_more = False
    for i, channel_batch in enumerate(channel_batches, 1):
        if i > 1 and inter_batch_sleep > 0:
            _sleep(inter_batch_sleep)
        logger.info(
            "wb-mqtt-db get_values RPC call %d/%d: channels=%d uid>%d limit=%d",
            i,
            len(channel_batches),
            len(channel_batch),
            last_uid,
            max_records,
        )
        params = {
            # ver=0 intentionally returns device/control for every row.
            # ver=1 returns internal channel id in `c`, but get_channels() does not
            # expose that id, so mapping `c` back to topic would be unreliable.
            "ver": 0,
            "channels": [channel["pair"] for channel in channel_batch],
            "uid": {"gt": int(last_uid)},
            "limit": int(max_records),
            "request_timeout": RPC_TIMEOUT_SECONDS,
        }
        result = _call_history_get_values(rpc, params)
        values.extend(result.get("values", []))
        has_more = has_more or bool(result.get("has_more"))

    sorted_values = sorted(values, key=value_uid)
    if len(sorted_values) > max_records:
        has_more = True
        sorted_values = sorted_values[:max_records]

    return {"has_more": has_more, "values": sorted_values}


def value_uid(value: dict[str, Any]) -> int:
    """Extract monotonically increasing wb-mqtt-db UID from a value row."""
    return int(value.get("uid", 0))


def value_timestamp_ns(value: dict[str, Any], topic: str = "") -> int:
    """Convert timestamp to nanoseconds; topics in STATIC_RETAINED_TOPICS always get now().

    Static topics have their original (stale) publication timestamp, which TimescaleDB's
    retention policy would silently drop — so we replace it with the current time.
    """
    timestamp = value.get("timestamp")
    if timestamp is None or topic in STATIC_RETAINED_TOPICS:
        return int(time.time() * 1_000_000_000)
    return int(float(timestamp) * 1_000_000_000)


def value_topic(value: dict[str, Any]) -> str:
    """Resolve canonical full MQTT topic from a wb-mqtt-db value row (ver=0 format)."""
    if value.get("device") and value.get("control"):
        return f"/devices/{value['device']}/controls/{value['control']}"
    raise ValueError(f"Cannot resolve topic for value {value!r}")


def value_to_influx_line(value: dict[str, Any]) -> str:
    """Build one Influx Line Protocol row for server-side Telegraf ingestion."""
    measurement = MEASUREMENT
    topic = value_topic(value)
    tags = [f"topic={escape_tag(topic)}"]
    fields = [f'value="{escape_field_string(value.get("value", ""))}"']
    return f"{measurement},{','.join(tags)} {','.join(fields)} {value_timestamp_ns(value, topic)}"


def gzip_payload(lines: list[str]) -> bytes:
    """Compress Influx lines with fast gzip level to reduce controller traffic."""
    data = ("\n".join(lines) + "\n").encode("utf-8")
    return gzip.compress(data, compresslevel=1)


def _run_curl(command: list[str], payload: bytes) -> str:
    """Execute one curl attempt; return HTTP status code string.

    Raises RuntimeError on timeout, certificate, network, or other curl errors.
    """
    try:
        result = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            check=False,
            timeout=CURL_MAX_TIME_SECONDS + 5,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"curl timed out after {CURL_MAX_TIME_SECONDS + 5}s while sending metrics"
        ) from exc
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    # Certificate-level errors: 58=bad client cert, 60=SSL verify failed, 77=CA cert not found
    if result.returncode in (58, 60, 77):
        raise RuntimeError(
            f"Certificate error (curl {result.returncode}) while sending metrics (cert={CERT_FILE}): {stderr}"
        )
    # Transport-level errors — same codes as in cloud-agent curl handler
    if result.returncode in (6, 7, 28):
        raise RuntimeError(f"Network error while sending metrics (curl code={result.returncode}): {stderr}")
    if result.returncode != 0:
        raise RuntimeError(f"curl error (code={result.returncode}): {stderr}")
    return result.stdout.decode("utf-8", errors="replace").strip()


@log_duration("metrics send")
def send_lines(lines: list[str]) -> None:
    """Send compressed Influx Line Protocol batch to cloud metrics endpoint."""
    if not lines:
        return

    logger.info("Sending %d ILP lines to %s", len(lines), METRICS_URL)
    # No --fail: curl exits 0 on any completed HTTP transfer; we check http_code ourselves.
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--connect-timeout",
        str(CURL_CONNECT_TIMEOUT_SECONDS),
        "--max-time",
        str(CURL_MAX_TIME_SECONDS),
        "--cert",
        CERT_FILE,
        "--key",
        CRYPTO_ENGINE_KEY,
        *(["--engine", CRYPTO_ENGINE, "--key-type", "ENG"] if CRYPTO_ENGINE else []),
        "-H",
        "Content-Type: text/plain",
        "-H",
        "Content-Encoding: gzip",
        "--data-binary",
        "@-",
        METRICS_URL,
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
    ]
    payload = gzip_payload(lines)
    if len(payload) > MAX_REQUEST_BYTES:
        raise RuntimeError(
            f"Batch too large: {len(payload)} compressed bytes exceeds MAX_REQUEST_BYTES={MAX_REQUEST_BYTES}"
            f" ({len(lines)} lines) — reduce SEND_BATCH_SIZE"
        )
    for attempt in range(SEND_MAX_RETRIES):
        http_code = _run_curl(command, payload)
        if http_code.startswith("2"):
            logger.info("Metrics sent: HTTP %s", http_code)
            return
        if http_code == "413":
            raise RuntimeError(f"Batch too large (HTTP 413): {len(lines)} lines — reduce SEND_BATCH_SIZE")
        if http_code == "429" and attempt < SEND_MAX_RETRIES - 1:
            logger.warning(
                "Rate limited (HTTP 429), retrying in %ds (attempt %d/%d)",
                SEND_RATE_LIMIT_RETRY_DELAY_SECONDS,
                attempt + 1,
                SEND_MAX_RETRIES,
            )
            _sleep(SEND_RATE_LIMIT_RETRY_DELAY_SECONDS)
            continue
        raise RuntimeError(f"HTTP {http_code} error while sending metrics to {METRICS_URL}")


def _fetch_new_values(
    rpc: Any, channels: list[dict[str, Any]], last_uid: int, catch_up: bool
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch values newer than last_uid; return (sorted_values, has_more)."""
    max_records = CATCH_UP_MAX_RECORDS if catch_up else MAX_RECORDS
    inter_batch_sleep = CATCH_UP_BATCH_SLEEP_SECONDS if catch_up else 0.0
    result = get_values(rpc, channels, last_uid, max_records, inter_batch_sleep)
    values = sorted(result.get("values", []), key=value_uid)
    return values, bool(result.get("has_more")) and bool(values)


def _build_ilp_pairs(values: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Convert raw db values to (uid, ILP-line) pairs; skip malformed entries with a warning."""
    uid_line_pairs: list[tuple[int, str]] = []
    for value in values:
        try:
            uid_line_pairs.append((value_uid(value), value_to_influx_line(value)))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Skipping malformed value (uid=%s): %s", value_uid(value), exc)
    return uid_line_pairs


def _send_batches(uid_line_pairs: list[tuple[int, str]], static_lines: list[str] | None = None) -> None:
    """Send uid_line_pairs in SEND_BATCH_SIZE sub-batches; save uid progress after each.

    If ``static_lines`` are provided they are prepended to the first batch so that
    static and dynamic data are sent in a single HTTP request, staying within the
    Traefik HTTP rate limit (two consecutive requests trigger HTTP 429).
    """
    for i, batch_start in enumerate(range(0, len(uid_line_pairs), SEND_BATCH_SIZE)):
        batch = uid_line_pairs[batch_start : batch_start + SEND_BATCH_SIZE]
        lines = [line for _, line in batch]
        if i == 0 and static_lines:
            logger.info("Static values merged into first batch (%d lines)", len(static_lines))
            lines = static_lines + lines
        send_lines(lines)
        batch_max_uid = batch[-1][0]
        save_last_uid(batch_max_uid)
        logger.info("Progress saved: last_uid=%s", batch_max_uid)


def _maybe_refresh_static_lines(mqtt_client: Any, now: float) -> tuple[list[str], bool]:
    """Re-read static retained MQTT topics if STATIC_REFRESH_INTERVAL has elapsed.

    Returns ``(static_lines, did_refresh)``. The static topics predate ``start_uid`` and
    may have been evicted from wb-mqtt-db, so we read them directly from the broker
    periodically and merge them into the next dynamic batch (single HTTP request per
    cycle stays within the Traefik rate limit).
    """
    if now - _static_last_refresh < STATIC_REFRESH_INTERVAL:
        return [], False
    static_values = get_static_values_from_mqtt(mqtt_client)
    if not static_values:
        return [], True
    static_pairs = _build_ilp_pairs(static_values)
    return [line for _, line in static_pairs], True


def _log_metrics_gap_if_any(last_uid: int, values: list[dict[str, Any]]) -> None:
    """Detect and WARN about uid holes between the previously-saved last_uid and this batch."""
    first_uid = value_uid(values[0])
    max_uid = value_uid(values[-1])
    if last_uid and first_uid > last_uid + 1:
        lost_count = first_uid - last_uid - 1
        logger.warning(
            "Metrics gap detected: lost uid range %s..%s (%d records)",
            last_uid + 1,
            first_uid - 1,
            lost_count,
        )
    logger.info("Fetched %d values (uid %s..%s)", len(values), first_uid, max_uid)


def collect_once(rpc: Any, mqtt_client: Any, catch_up: bool) -> bool:
    """Collect one batch from wb-mqtt-db, send it, and persist progress on success."""
    global _static_last_refresh
    last_uid = load_last_uid()
    channels = get_cached_channels(rpc)
    if not channels:
        logger.info("No channels returned by wb-mqtt-db")
        return False

    if last_uid == 0:
        last_uid = find_start_uid(rpc, channels)
        if last_uid:
            save_last_uid(last_uid)

    now = time.time()
    static_lines, did_static_refresh = _maybe_refresh_static_lines(mqtt_client, now)

    logger.info("Collecting metrics (last_uid=%s, catch_up=%s)", last_uid, catch_up)
    values, has_more = _fetch_new_values(rpc, channels, last_uid, catch_up)
    if not values:
        if static_lines:
            send_lines(static_lines)
            logger.info("Static values sent (%d lines)", len(static_lines))
        elif not did_static_refresh:
            logger.debug("No new values since uid=%s", last_uid)
        if did_static_refresh:
            _static_last_refresh = now
        return False

    _log_metrics_gap_if_any(last_uid, values)

    uid_line_pairs = _build_ilp_pairs(values)
    if not uid_line_pairs:
        max_uid = value_uid(values[-1])
        logger.warning(
            "All %d fetched values were malformed — advancing uid to %s without sending",
            len(values),
            max_uid,
        )
        save_last_uid(max_uid)
        if did_static_refresh:
            _static_last_refresh = now
        return has_more

    # Static lines are merged into the first batch to avoid a second HTTP request.
    _send_batches(uid_line_pairs, static_lines or None)
    if did_static_refresh:
        _static_last_refresh = now
    return has_more


def get_service_state(service_name: str) -> str | None:
    """Return the systemd ``ActiveState`` of the specified service (``active``, ``failed`` etc.).

    Uses ``systemctl is-active`` because it is authoritative and answers in tens of
    milliseconds — far cheaper than waiting another full RPC timeout to guess whether
    the service is dead. Returns ``None`` if we can't run systemctl at all (missing
    binary, sandboxed environment); the caller then falls through to its normal path.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            timeout=SERVICE_STATE_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Cannot determine %s state: %s", service_name, exc)
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() or None


def _wait_until_service_active() -> bool:
    """Probe systemd once; return True if service is back, otherwise log+sleep and return False.

    Used in the skip-mode top-of-loop so we spend ~50ms on a systemctl call per cycle
    instead of burning a full RPC timeout on a controller in crash-loop.
    """
    state = get_service_state(WB_MQTT_DB_SERVICE_NAME)
    if state == "active":
        logger.info("%s is active again — resuming metrics collection", WB_MQTT_DB_SERVICE_NAME)
        return True
    if state is None:
        # systemctl unavailable — don't stay stuck in skip-mode forever. Fall back to
        # the normal RPC path; _handle_rpc_timeout will re-evaluate on the next timeout.
        logger.warning(
            "%s state cannot be determined (systemctl unavailable) — "
            "exiting skip-mode and falling back to RPC retry",
            WB_MQTT_DB_SERVICE_NAME,
        )
        return True
    logger.warning(
        "%s is '%s' on the controller — skipping cycle (no RPC attempted).",
        WB_MQTT_DB_SERVICE_NAME,
        state,
    )
    _sleep(INTERVAL_SECONDS)
    return False


def _handle_rpc_timeout(
    mqtt_client: Any, rpc: Any, mqtt_state: MQTTConnectionState
) -> tuple[Any, Any, MQTTConnectionState, bool]:
    """Decide what to do after MQTTRPCTimeoutError; return updated (mqtt_client, rpc, skip).

    Three outcomes:
    - Broker link is down → reconnect to a fresh client.
    - Broker OK, systemd says service is non-active → enter skip-mode (caller stops RPCs).
    - Broker OK, systemd says service is active (or systemctl unavailable) → just log.
    """
    exit_code = mqtt_state.requested_exit_code()
    if exit_code is not None:
        raise CollectorExit(exit_code)

    if not mqtt_client.is_connected():
        logger.error("wb-mqtt-db RPC timed out and MQTT broker connection is down — reconnecting")
        mqtt_client, rpc, mqtt_state = reconnect_mqtt_rpc(mqtt_client)
        return mqtt_client, rpc, mqtt_state, False

    state = get_service_state(WB_MQTT_DB_SERVICE_NAME)
    if state is not None and state != "active":
        logger.warning(
            "wb-mqtt-db RPC timed out and %s is '%s' — service is DOWN on the controller. "
            "Pausing RPC attempts until it is restored.",
            WB_MQTT_DB_SERVICE_NAME,
            state,
        )
        return mqtt_client, rpc, mqtt_state, True

    logger.error(
        "wb-mqtt-db RPC timed out: wb-mqtt-db service is slow or not responding. "
        "Broker still connected - retrying next cycle."
    )
    return mqtt_client, rpc, mqtt_state, False


def _sleep(seconds: float) -> None:
    """Sleep until the timeout expires or a service stop is requested."""
    if _STOP_REQUESTED.wait(seconds):
        raise CollectorExit(EXIT_STOPPED)


def run_forever() -> int:
    """Run collector loop until a controlled exit; systemd handles fatal exits."""
    catch_up = False
    # Once we've confirmed wb-mqtt-db is DOWN on the controller we stop attempting RPC
    # entirely — each cycle we just probe systemd until the service comes back.
    skip_until_active = False
    mqtt_client = None
    exit_code = EXIT_FAILURE
    try:
        mqtt_client, rpc, mqtt_state = connect_mqtt_rpc()
        rpc_connection_generation = mqtt_state.connection_generation()
        while True:
            requested_exit = mqtt_state.requested_exit_code()
            if requested_exit is not None:
                raise CollectorExit(requested_exit)

            rpc, rpc_connection_generation = refresh_rpc_client_after_reconnect(
                mqtt_client,
                rpc,
                mqtt_state,
                rpc_connection_generation,
            )

            if skip_until_active:
                if _wait_until_service_active():
                    skip_until_active = False
                else:
                    catch_up = False
                    continue

            try:
                catch_up = collect_once(rpc, mqtt_client, catch_up)
            except MQTTRPCTimeoutError:
                catch_up = False
                previous_mqtt_state = mqtt_state
                mqtt_client, rpc, mqtt_state, skip_until_active = _handle_rpc_timeout(
                    mqtt_client, rpc, mqtt_state
                )
                if mqtt_state is not previous_mqtt_state:
                    rpc_connection_generation = mqtt_state.connection_generation()
            except CollectorExit:
                raise
            except Exception as exc:  # pylint: disable=broad-except
                catch_up = False
                logger.exception("Metrics iteration failed: %s", exc)
                mqtt_client, rpc, mqtt_state = reconnect_mqtt_rpc(mqtt_client)
                rpc_connection_generation = mqtt_state.connection_generation()

            sleep_seconds = CATCH_UP_SLEEP_SECONDS if catch_up else INTERVAL_SECONDS
            logger.debug("Sleeping %ds before next iteration", sleep_seconds)
            requested_exit = mqtt_state.wait_for_exit(sleep_seconds)
            if requested_exit is not None:
                raise CollectorExit(requested_exit)
    except CollectorExit as exc:
        exit_code = exc.exit_code
        return exit_code
    finally:
        if mqtt_client is not None:
            stop_mqtt_client(mqtt_client, report_unavailable=exit_code == EXIT_STOPPED)


def _handle_stop_signal(signum: int, _frame: Any) -> None:
    if not _STOP_REQUESTED.is_set():
        logger.info("Got signal %s, stopping metrics collector", signal.Signals(signum).name)
    _STOP_REQUESTED.set()


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the systemd metrics collector service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    _STOP_REQUESTED.clear()
    previous_handlers = {
        signum: signal.signal(signum, _handle_stop_signal) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    logger.info("Starting metrics collector version=%s created_at=%s", VERSION, CREATED_AT)
    if not Path(CERT_FILE).exists():
        logger.warning(
            "Certificate file not found: %s — curl will fail until the agent provisions it",
            CERT_FILE,
        )
    try:
        return run_forever()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
