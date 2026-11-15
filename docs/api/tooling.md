# Tooling

<a id="hostenv"></a>

## hostenv

Look a setting up in the environment, then in this repo's .env.

Host-run scripts do not load .env themselves; compose does that for the containers.
This module never writes the environment or the .env file, and never prints a
value it has read.

<a id="hostenv.get"></a>

### get

```python
def get(name, required=True)
```

Return a setting from the environment, falling back to this repo's .env.

**Arguments**:

- `name` - Variable name to look up.
- `required` - When true, exit the process if the setting is absent.
  

**Returns**:

  The value, with surrounding quotes stripped for .env values, or None
  when it is absent and not required.
  

**Raises**:

- `SystemExit` - The setting is missing and ``required`` is true.

<a id="tasks"></a>

## tasks

Development task entry points run through ``uv run poe``.

This module only sets up the local development environment. It must never
touch the live warehouse, QuestDB or any deployed service.

<a id="tasks.setup"></a>

### setup

```python
def setup() -> None
```

Set up the development environment.

Creates a default .env if missing, syncs dependencies with uv and installs
pre-commit hooks when run inside a git checkout.

**Raises**:

- `SystemExit` - A setup subprocess failed (exit code 1).

