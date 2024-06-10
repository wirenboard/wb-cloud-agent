#!/bin/bash

set -e

for provider in $(ls /mnt/data/etc/wb-cloud-agent/providers/); do
    if ! systemctl -q is-enabled wb-cloud-agent@$provider; then
        systemctl enable wb-cloud-agent@$provider
        systemctl restart wb-cloud-agent@$provider
    fi
done
