#!/bin/bash

set -e

AGENT_CONFIG="${1:-/etc/wb-cloud-agent.conf}"

print_bundle_part() {
    awk -v "req_part=$1" '/BEGIN CERT/{c++} c == req_part { print }'
}

cert_is_valid() {
    (openssl x509 -in "$1" -noout -subject || true) | grep -q "Production"
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

fix_engine_key() {
    local config_path="$1"
    local engine_key="$2"
    python3 - "$config_path" "$engine_key" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1]).resolve()
engine_key = sys.argv[2]
try:
    if not config_path.is_file() or not config_path.stat().st_size:
        raise SystemExit(0)
    old_stat = config_path.stat()
    contents = config_path.read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit(0)

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

# Only touch a parseable config: a damaged one is quarantined and restored by the agent
# itself (recover_provider_config), which also sets the right engine key.
config_is_json() {
    python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$1" 2>/dev/null
}

. /usr/lib/wb-utils/wb_env.sh
wb_source of

if of_machine_match "contactless,imx6ul-wirenboard60"; then
    ENGINE_KEY="ATECCx08:00:04"
else
    # Both WB7, WB8 have atecc on i2c2
    ENGINE_KEY="ATECCx08:00:02"
fi

if [ ! -s "$AGENT_CONFIG" ]; then
    echo "Config $AGENT_CONFIG is missing or empty, the agent will restore it"
elif ! config_is_json "$AGENT_CONFIG"; then
    echo "Config $AGENT_CONFIG is malformed, the agent will restore it"
else
    fix_engine_key "$AGENT_CONFIG" "$ENGINE_KEY"
fi
