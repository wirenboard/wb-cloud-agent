#!/bin/bash

set -e

print_bundle_part() {
    awk -v "req_part=$1" 'BEGIN { c = 0; } /BEGIN CERT/{c++} c == req_part { print }'
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
