#!/bin/bash

set -e

AGENT_CONFIG="${1:-/etc/wb-cloud-agent.conf}"

print_bundle_part() {
    awk -v "req_part=$1" '/BEGIN CERT/{c++} c == req_part { print }'
}

cert_is_valid() {
    (openssl x509 -in "$1" -noout -subject || true) | grep -q "Production"
}

fix_engine_key() {
    python3 - "$AGENT_CONFIG" "$1" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1]).resolve()
engine_key = sys.argv[2]
old_stat = config_path.stat()
contents = config_path.read_text(encoding="utf-8")
updated = re.sub(r"ATECCx08:00:..", engine_key, contents)
if updated == contents:
    raise SystemExit(0)

fd, temp_name = tempfile.mkstemp(prefix=f".{config_path.name}.tmp-", dir=config_path.parent)
temp_path = Path(temp_name)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        os.fchown(temp_file.fileno(), old_stat.st_uid, old_stat.st_gid)
        os.fchmod(temp_file.fileno(), stat.S_IMODE(old_stat.st_mode))
        temp_file.write(updated)
        temp_file.flush()
        os.fsync(temp_file.fileno())
    os.replace(temp_path, config_path)
    dir_fd = os.open(config_path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
finally:
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
PY
}

ORIGINAL_CERT=/etc/ssl/certs/device_bundle.crt.pem
TARGET_CERT=/var/lib/wb-cloud-agent/device_bundle.crt.pem

if [ ! -f "$ORIGINAL_CERT" ]; then
    echo "Can't find device certificate!"
    exit 1
fi

mkdir -p /var/lib/wb-cloud-agent

# create correct certificate for agent to use
if [ ! -f "$TARGET_CERT" ] || ! cert_is_valid "$TARGET_CERT"; then
    if cert_is_valid "$ORIGINAL_CERT"; then
        echo "Device cert is OK, reusing it"
        rm -f "$TARGET_CERT"
        ln -s "$ORIGINAL_CERT" "$TARGET_CERT"
    else
        echo "Creating fixed bundle certificate"
        print_bundle_part 2 < "$ORIGINAL_CERT" > "$TARGET_CERT"
        print_bundle_part 1 < "$ORIGINAL_CERT" >> "$TARGET_CERT"
    fi
fi

if [ ! -s "$AGENT_CONFIG" ]; then
    echo "Config $AGENT_CONFIG is missing or empty, the agent will restore it"
    exit 0
fi

# Skip malformed configs so run-daemon can quarantine the original.
if ! python3 - "$AGENT_CONFIG" <<'PY'
import json
import sys
from urllib.parse import urlparse

try:
    with open(sys.argv[1], encoding="utf-8") as config:
        value = json.load(config)
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)

if not isinstance(value, dict):
    raise SystemExit(1)

cloud_base_url = value.get("CLOUD_BASE_URL")
if "CLOUD_BASE_URL" in value and (
    not isinstance(cloud_base_url, str)
    or not cloud_base_url.strip()
    or urlparse(cloud_base_url).scheme not in ("http", "https")
    or not urlparse(cloud_base_url).netloc
):
    raise SystemExit(1)
if "LOG_LEVEL" in value and not isinstance(value["LOG_LEVEL"], str):
    raise SystemExit(1)
for key in ("CLIENT_CERT_ENGINE_KEY", "CLIENT_CERT_FILE", "BROKER_URL"):
    if key in value and (not isinstance(value[key], str) or not value[key].strip()):
        raise SystemExit(1)
for key in ("REQUEST_PERIOD_SECONDS", "PING_PERIOD_SECONDS"):
    if key in value and (
        isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] <= 0
    ):
        raise SystemExit(1)
if "METRICS_LOG_ENABLED" in value and not isinstance(value["METRICS_LOG_ENABLED"], bool):
    raise SystemExit(1)
PY
then
    echo "Config $AGENT_CONFIG is malformed, the agent will restore it"
    exit 0
fi

. /usr/lib/wb-utils/wb_env.sh
wb_source of

if of_machine_match "contactless,imx6ul-wirenboard60"; then
    fix_engine_key "ATECCx08:00:04"
else
    # Both WB7, WB8 have atecc on i2c2
    fix_engine_key "ATECCx08:00:02"
fi
