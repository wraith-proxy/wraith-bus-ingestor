#!/bin/sh
# Ghost Stack Test Suite & Coverage Enforcer
#
# The coverage gate itself lives in pyproject.toml (--cov-fail-under), not
# here, so this script and CI never need to change when the threshold does.

if [ "$1" = "--no-venv" ]; then
    echo "--- CI Mode: Using System Python ---"
    pytest
else
    echo "--- Local Mode: Using uv run ---"
    uv run pytest
fi

RESULT=$?

if [ $RESULT -eq 0 ]; then
    echo "SUCCESS: Tests passed and coverage is sufficient."
else
    echo "FAILURE: Tests failed or coverage is below the gate."
    exit 1
fi
