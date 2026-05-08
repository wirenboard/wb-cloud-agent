UNKNOWN_LINK = "unknown"
NOCONNECT_LINK = "noconnect"

DEFAULT_PROVIDER_CONF_FILE = "/etc/wb-cloud-agent.conf"
PROVIDERS_CONF_DIR = "/etc/wb-cloud-agent/providers"

APP_DATA_DIR = "/var/lib/wb-cloud-agent"
APP_DATA_PROVIDERS_DIR = f"{APP_DATA_DIR}/providers"

CLOUD_AGENT_URL_POSTFIX = "/api-agent/v1/"

CLIENT_CERT_ERROR_MSG = (
    "Cert {cert_file} and key {cert_engine_key} "
    "seem to be inconsistent (possibly because of CPU board missmatch)!"
)

UNBIND_CTRL_REQUEST_TIMEOUT = 7

# Health monitoring for metrics collector service after update delivery.
# After the script is deployed and the service is restarted, a background daemon
# thread monitors the service for METRICS_HEALTH_CHECK_INTERVAL_S * METRICS_HEALTH_CHECK_COUNT
# seconds. A report is sent to the cloud only when a genuine persistent failure is detected.
METRICS_HEALTH_CHECK_INTERVAL_S = 600  # run a check every 10 minutes
METRICS_HEALTH_CHECK_COUNT = 6  # monitor for 60 minutes total
# Errors per 10-min window to consider that window "bad".
# metrics_collector sleeps 120 s after each error, so max ~5 errors/window;
# threshold 3 means at least 3 consecutive failures (not a one-off glitch).
METRICS_HEALTH_ERROR_WINDOW_THRESHOLD = 3
# Consecutive "bad" windows before sending a report (3 windows = 30 min).
METRICS_HEALTH_CONSECUTIVE_ERROR_WINDOWS = 3
METRICS_HEALTH_JOURNAL_LINES = 200  # lines to include in the report
METRICS_HEALTH_JOURNAL_MAX_BYTES = 8192  # byte cap on the report log
# Marker text that distinguishes ERROR-level entries from metrics_collector.py.
# The script uses format="%(levelname)s wb-cloud-metrics: ...", so this string
# appears only in genuine ERROR/EXCEPTION calls, not in WARNING or INFO lines.
METRICS_HEALTH_ERROR_MARKER = "ERROR wb-cloud-metrics:"
