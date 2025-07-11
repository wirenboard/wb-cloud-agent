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
