#!/bin/bash

set -e

if [ -d /etc/wb-cloud-agent/providers ]; then
    for provider in $(ls /etc/wb-cloud-agent/providers/); do
        if ! systemctl -q is-enabled wb-cloud-agent@$provider; then
            systemctl enable wb-cloud-agent@$provider
            systemctl restart wb-cloud-agent@$provider
        fi
    done
fi
