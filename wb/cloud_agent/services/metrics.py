from __future__ import annotations

import json
import logging
import os
import random
import string
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from wb.cloud_agent.constants import (
    METRICS_COLLECTOR_CRYPTO_ENGINE,
    METRICS_COLLECTOR_TEMPLATE_PATH,
    METRICS_HEALTH_CHECK_COUNT,
    METRICS_HEALTH_CHECK_INTERVAL_S,
    METRICS_HEALTH_CONSECUTIVE_ERROR_WINDOWS,
    METRICS_HEALTH_ERROR_MARKER,
    METRICS_HEALTH_ERROR_WINDOW_THRESHOLD,
    METRICS_HEALTH_JOURNAL_LINES,
    METRICS_HEALTH_JOURNAL_MAX_BYTES,
    METRICS_TEMPLATE_CLOUD_VARS,
    UNKNOWN_LINK,
)
from wb.cloud_agent.handlers.curl import CloudNetworkError, do_curl
from wb.cloud_agent.mqtt import MQTTCloudAgent
from wb.cloud_agent.services.activation import write_activation_link
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import (
    start_and_enable_service,
    stop_and_disable_service,
    write_to_file,
)

_monitor_threads: dict[str, threading.Thread] = {}
_monitor_stop_events: dict[str, threading.Event] = {}


def _safe_stop_and_disable_service(service: str) -> None:
    try:
        stop_and_disable_service(service)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logging.warning("Cannot stop service %s: %s", service, exc)


def _ensure_service_is_active(service: str) -> None:
    subprocess.run(["systemctl", "is-active", "--quiet", service], check=True, timeout=10)


def _is_service_failed(service: str) -> bool:
    """Return True only when systemd reports the service is in the 'failed' state.

    Uses 'is-failed' instead of 'is-active' to avoid false positives from
    transient 'activating' or 'deactivating' states during normal restarts.
    """
    result = subprocess.run(
        ["systemctl", "is-failed", "--quiet", service],
        check=False,
        timeout=10,
    )
    return result.returncode == 0


def _collect_service_journal(service: str, since_seconds: int) -> str:
    """Return the last journal lines, capped at METRICS_HEALTH_JOURNAL_MAX_BYTES."""
    result = subprocess.run(
        [
            "journalctl",
            "-u",
            service,
            f"--since={since_seconds} seconds ago",
            "--no-pager",
            "-n",
            str(METRICS_HEALTH_JOURNAL_LINES),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    return result.stdout[:METRICS_HEALTH_JOURNAL_MAX_BYTES].decode("utf-8", errors="ignore")


def _count_collector_errors(journal: str) -> int:
    """Count ERROR-level entries written by metrics_collector.py.

    metrics_collector uses format='%(levelname)s wb-cloud-metrics: ...', so
    METRICS_HEALTH_ERROR_MARKER uniquely identifies its ERROR/EXCEPTION lines.
    WARNING lines ('Metrics gap detected', 'Cannot read state file') are
    intentionally excluded because they represent non-fatal conditions.
    """
    return sum(1 for line in journal.splitlines() if METRICS_HEALTH_ERROR_MARKER in line)


def _report_metrics_health(settings: AppSettings, reason: str, log: str) -> bool:
    if not settings.metrics_log_enabled:
        logging.info("Metrics log reporting is disabled, skipping (reason=%s)", reason)
        return False
    try:
        _event_data, status_code = do_curl(
            settings,
            method="post",
            endpoint="metrics-collector-log/",
            params={"reason": reason, "log": log},
            retry_opts=["--connect-timeout", "15", "--retry", "2", "--retry-delay", "5"],
            compress_request_body=True,
        )
        if status_code >= 400:
            raise RuntimeError(f"metrics health endpoint returned HTTP {status_code}")
        logging.info("Reported metrics health (reason=%s)", reason)
        return True
    except (
        CloudNetworkError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        logging.warning("Failed to report metrics health: %s", exc)
        return False


def report_metrics_health(settings: AppSettings, reason: str, log: str) -> None:
    if _report_metrics_health(settings, reason, log):
        logging.info(
            "Stopping metrics health monitor for provider %s after reporting metrics health (reason=%s)",
            settings.provider_name,
            reason,
        )


def _monitor_metrics_service(settings: AppSettings, service: str, stop_event: threading.Event) -> None:
    """Monitor the metrics service after a config update for up to 60 minutes.

    A report is sent only when a genuine persistent failure is detected:
      - service_failed: systemd considers the service dead (all restart attempts
        exhausted). Triggered on the first check that finds the service in 'failed' state.
      - persistent_errors: METRICS_HEALTH_CONSECUTIVE_ERROR_WINDOWS consecutive
        10-minute windows each contain at least METRICS_HEALTH_ERROR_WINDOW_THRESHOLD
        ERROR-level lines from the collector script. Filters transient glitches;
        catches persistent error loops.
    """
    try:
        consecutive_error_windows = 0
        for _ in range(METRICS_HEALTH_CHECK_COUNT):
            if stop_event.wait(METRICS_HEALTH_CHECK_INTERVAL_S):
                logging.info(
                    "Metrics service monitor for %s was restarted by a newer config update",
                    service,
                )
                return

            if _is_service_failed(service):
                logging.warning("Metrics service %s entered failed state", service)
                total_seconds = METRICS_HEALTH_CHECK_INTERVAL_S * METRICS_HEALTH_CHECK_COUNT
                log = _collect_service_journal(service, total_seconds)
                report_metrics_health(settings, "service_failed", log)
                return

            journal = _collect_service_journal(service, METRICS_HEALTH_CHECK_INTERVAL_S)
            error_count = _count_collector_errors(journal)

            if error_count >= METRICS_HEALTH_ERROR_WINDOW_THRESHOLD:
                consecutive_error_windows += 1
                logging.warning(
                    "Metrics service %s: error window %d/%d (%d errors)",
                    service,
                    consecutive_error_windows,
                    METRICS_HEALTH_CONSECUTIVE_ERROR_WINDOWS,
                    error_count,
                )
                if consecutive_error_windows >= METRICS_HEALTH_CONSECUTIVE_ERROR_WINDOWS:
                    report_seconds = (
                        METRICS_HEALTH_CHECK_INTERVAL_S * METRICS_HEALTH_CONSECUTIVE_ERROR_WINDOWS
                    )
                    log = _collect_service_journal(service, report_seconds)
                    report_metrics_health(settings, "persistent_errors", log)
                    return
            else:
                if consecutive_error_windows > 0:
                    logging.info(
                        "Metrics service %s recovered (reset after %d error window(s))",
                        service,
                        consecutive_error_windows,
                    )
                consecutive_error_windows = 0

        logging.info(
            "Stopping metrics health monitor for provider %s after monitoring window completed",
            settings.provider_name,
        )
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        logging.warning("Metrics health monitor failed unexpectedly: %s", exc)
    finally:
        if _monitor_stop_events.get(settings.provider_name) is stop_event:
            _monitor_stop_events.pop(settings.provider_name, None)
        if _monitor_threads.get(settings.provider_name) is threading.current_thread():
            _monitor_threads.pop(settings.provider_name, None)


def _escape_template_value(value: object) -> object:
    """Neutralise a string so it cannot break out of the ``"..."`` literal it lands in.

    ``json.dumps`` returns a valid quoted string with quotes and backslashes escaped;
    dropping the outer quotes keeps the escaped contents. Non-strings pass through and
    rely on the ``int()``/``float()`` wrappers in the template.
    """
    if isinstance(value, str):
        return json.dumps(value)[1:-1]
    return value


def _generate_mqtt_client_id() -> str:
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return f"wb-cloud-agent-metrics-{suffix}"


def _load_vars_conf(settings: AppSettings) -> dict | None:
    try:
        return json.loads(settings.metrics_vars_config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logging.warning("Cannot read metrics vars config %s: %s", settings.metrics_vars_config, exc)
        return None


def _build_vars_conf(settings: AppSettings, raw_vars: dict) -> dict:
    # Keep only known cloud variables; everything else the agent fills in itself.
    existing = _load_vars_conf(settings) or {}
    return {
        "vars": {key: raw_vars[key] for key in METRICS_TEMPLATE_CLOUD_VARS if key in raw_vars},
        "mqtt_client_id": existing.get("mqtt_client_id") or _generate_mqtt_client_id(),
        "created_at": datetime.now(timezone.utc).isoformat(sep=" "),
    }


def render_metrics_script(settings: AppSettings, conf: dict) -> str:
    # Agent-owned values are applied last so a payload cannot override them
    # (e.g. inject its own crypto_engine_key).
    substitution = dict(conf.get("vars", {}))
    substitution.update(
        created_at=conf["created_at"],
        mqtt_broker_url=settings.broker_url,
        mqtt_client_id=conf["mqtt_client_id"],
        crypto_engine=METRICS_COLLECTOR_CRYPTO_ENGINE,
        crypto_engine_key=settings.client_cert_engine_key,
        state_file=str(settings.metrics_last_uid),
    )
    template = Path(METRICS_COLLECTOR_TEMPLATE_PATH).read_text(encoding="utf-8")
    return Template(template).substitute(
        {key: _escape_template_value(value) for key, value in substitution.items()}
    )


def reconcile_metrics_script(settings: AppSettings) -> None:
    """Re-render the collector script from the bundled template on agent start.

    Lets a script fix shipped in a new package version take effect without waiting for the
    cloud: the variables saved on the previous config update are reused. Does nothing until
    the cloud has sent the first config (no vars file yet).
    """
    try:
        conf = _load_vars_conf(settings)
        if not conf:
            return
        rendered = render_metrics_script(settings, conf)
        try:
            if settings.metrics_script.read_text(encoding="utf-8") == rendered:
                return
        except FileNotFoundError:
            pass
        logging.info(
            "Re-rendering metrics collector script from bundled template for provider %s",
            settings.provider_name,
        )
        write_to_file(fpath=settings.metrics_script, contents=rendered)
        os.chmod(settings.metrics_script, 0o755)
        start_and_enable_service(settings.metrics_service, restart=True)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        logging.warning(
            "Cannot reconcile metrics collector script for provider %s: %s", settings.provider_name, exc
        )


def update_metrics_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    if payload.get("enabled") is False:
        logging.info("Disabling metrics collection for provider %s", settings.provider_name)
        _monitor_stop_events.pop(settings.provider_name, threading.Event()).set()
        _safe_stop_and_disable_service(settings.metrics_service)
        write_activation_link(settings, UNKNOWN_LINK, mqtt)
        return

    # Security: the collector script ships with this package and is rendered locally from
    # cloud-supplied variables. A script delivered in the payload is never written or run.
    raw_vars = payload.get("vars")
    if not raw_vars:
        raise ValueError("Metrics config event payload has no variables")
    conf = _build_vars_conf(settings, raw_vars)
    write_to_file(fpath=settings.metrics_vars_config, contents=json.dumps(conf, indent=2))

    logging.info("Applying metrics collector config for provider %s", settings.provider_name)

    write_to_file(fpath=settings.metrics_script, contents=render_metrics_script(settings, conf))
    os.chmod(settings.metrics_script, 0o755)
    start_and_enable_service(settings.metrics_service, restart=True)
    try:
        _ensure_service_is_active(settings.metrics_service)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logging.warning(
            "Metrics service %s did not become active immediately: %s. Monitoring thread will track it.",
            settings.metrics_service,
            exc,
        )
    write_activation_link(settings, UNKNOWN_LINK, mqtt)

    existing = _monitor_threads.get(settings.provider_name)
    if existing is not None and existing.is_alive():
        logging.info("Restarting metrics health monitor for provider %s", settings.provider_name)
        _monitor_stop_events[settings.provider_name].set()

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_monitor_metrics_service,
        args=(settings, settings.metrics_service, stop_event),
        daemon=True,
        name=f"metrics-health-{settings.provider_name}",
    )
    _monitor_stop_events[settings.provider_name] = stop_event
    _monitor_threads[settings.provider_name] = thread
    logging.info("Started metrics health monitor for provider %s", settings.provider_name)
    thread.start()
