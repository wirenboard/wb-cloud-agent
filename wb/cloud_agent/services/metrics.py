import logging
import os
import subprocess
import threading
import time
from string import Template

from wb.cloud_agent.constants import (
    METRICS_HEALTH_CHECK_COUNT,
    METRICS_HEALTH_CHECK_INTERVAL_S,
    METRICS_HEALTH_CONSECUTIVE_ERROR_WINDOWS,
    METRICS_HEALTH_ERROR_MARKER,
    METRICS_HEALTH_ERROR_WINDOW_THRESHOLD,
    METRICS_HEALTH_JOURNAL_LINES,
    METRICS_HEALTH_JOURNAL_MAX_BYTES,
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
    """Return the last METRICS_HEALTH_JOURNAL_LINES lines, capped at METRICS_HEALTH_JOURNAL_MAX_BYTES."""
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
        text=True,
        timeout=15,
        check=False,
    )
    return result.stdout[:METRICS_HEALTH_JOURNAL_MAX_BYTES]


def _count_collector_errors(journal: str) -> int:
    """Count ERROR-level entries written by metrics_collector.py.

    metrics_collector uses format='%(levelname)s wb-cloud-metrics: ...', so
    METRICS_HEALTH_ERROR_MARKER uniquely identifies its ERROR/EXCEPTION lines.
    WARNING lines ('Metrics gap detected', 'Cannot read state file') are
    intentionally excluded because they represent non-fatal conditions.
    """
    return sum(1 for line in journal.splitlines() if METRICS_HEALTH_ERROR_MARKER in line)


def _report_metrics_health(settings: AppSettings, reason: str, log: str) -> None:
    if not settings.metrics_log_enabled:
        logging.info("Metrics log reporting is disabled, skipping (reason=%s)", reason)
        return
    try:
        do_curl(
            settings,
            method="post",
            endpoint="metrics-collector-log/",
            params={"reason": reason, "log": log},
            retry_opts=["--connect-timeout", "15", "--retry", "2", "--retry-delay", "5"],
        )
    except (CloudNetworkError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logging.warning("Failed to report metrics health: %s", exc)


def _monitor_metrics_service(settings: AppSettings, service: str) -> None:
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
            time.sleep(METRICS_HEALTH_CHECK_INTERVAL_S)

            if _is_service_failed(service):
                logging.warning("Metrics service %s entered failed state", service)
                total_seconds = METRICS_HEALTH_CHECK_INTERVAL_S * METRICS_HEALTH_CHECK_COUNT
                log = _collect_service_journal(service, total_seconds)
                _report_metrics_health(settings, "service_failed", log)
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
                    _report_metrics_health(settings, "persistent_errors", log)
                    return
            else:
                if consecutive_error_windows > 0:
                    logging.info(
                        "Metrics service %s recovered (reset after %d error window(s))",
                        service,
                        consecutive_error_windows,
                    )
                consecutive_error_windows = 0
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        logging.warning("Metrics health monitor failed unexpectedly: %s", exc)


def update_metrics_config(settings: AppSettings, payload: dict, mqtt: MQTTCloudAgent) -> None:
    if payload.get("enabled") is False:
        _safe_stop_and_disable_service(settings.metrics_service)
        write_activation_link(settings, UNKNOWN_LINK, mqtt)
        return

    if "script" not in payload:
        raise ValueError("Metrics config event payload has no collector script")

    write_to_file(
        fpath=settings.metrics_script,
        contents=Template(payload["script"]).safe_substitute(
            BROKER_URL=settings.broker_url,
            PROVIDER_NAME=settings.provider_name,
        ),
    )
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
    if existing is None or not existing.is_alive():
        thread = threading.Thread(
            target=_monitor_metrics_service,
            args=(settings, settings.metrics_service),
            daemon=True,
            name=f"metrics-health-{settings.provider_name}",
        )
        _monitor_threads[settings.provider_name] = thread
        thread.start()
