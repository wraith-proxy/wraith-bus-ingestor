"""Look a setting up in the environment, then in this repo's .env.

Host-run scripts do not load .env themselves; compose does that for the containers.
"""
import os
import sys
from pathlib import Path


def get(name, required=True):
    if name in os.environ:
        return os.environ[name]
    try:
        lines = (Path(__file__).resolve().parent / ".env").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"")
    if required:
        sys.exit(f"{name} is not set: add it to .env or export it")
    return None
