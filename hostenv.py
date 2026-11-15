"""Look a setting up in the environment, then in this repo's .env.

Host-run scripts do not load .env themselves; compose does that for the containers.
This module never writes the environment or the .env file, and never prints a
value it has read.
"""
import os
import sys
from pathlib import Path


def get(name, required=True):
    """Return a setting from the environment, falling back to this repo's .env.

    Args:
        name: Variable name to look up.
        required: When true, exit the process if the setting is absent.

    Returns:
        The value, with surrounding quotes stripped for .env values, or None
        when it is absent and not required.

    Raises:
        SystemExit: The setting is missing and ``required`` is true.
    """
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
