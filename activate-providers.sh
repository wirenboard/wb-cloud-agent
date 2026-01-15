#!/bin/bash

set -e

if [ -d /etc/wb-cloud-agent/providers ]; then
    for provider in $(ls /etc/wb-cloud-agent/providers/); do
        deb-systemd-invoke try-restart wb-cloud-agent@$provider
    done
fi
