#!/bin/sh -e

LD_LIBRARY_PATH="$(dirname "$0")" exec "$(dirname "$0")/curl" "$@"
