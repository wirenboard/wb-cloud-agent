#!/bin/bash

CONFIG_FILE="$1"
TOKEN_LINE="token = .*"

sed -i "/$TOKEN_LINE/d" "$CONFIG_FILE"