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

# fix agent config (ATECC path according to device version)
. /usr/lib/wb-utils/wb_env.sh
wb_source of

if of_machine_match "contactless,imx6ul-wirenboard60"; then
    sed -i --follow-symlinks 's/ATECCx08:00:../ATECCx08:00:04/g' "$AGENT_CONFIG"
else
    # Both WB7, WB8 have atecc on i2c2
    sed -i --follow-symlinks 's/ATECCx08:00:../ATECCx08:00:02/g' "$AGENT_CONFIG"
fi
