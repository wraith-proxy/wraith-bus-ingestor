import subprocess
import sys
from pathlib import Path

import typer


def setup() -> None:
    """Set up the development environment."""
    typer.echo("--- wraith-bus-ingestor ---")

    env_file = Path(".env")
    if not env_file.exists():
        typer.echo("Creating default .env from .env.example...")
        example = Path(".env.example")
        env_file.write_text(
            example.read_text()
            if example.exists()
            else "# wraith-bus-ingestor\n"
        )

    try:
        typer.echo("Syncing dependencies with uv...")
        subprocess.run(["uv", "sync", "--all-extras"], check=True)

        if Path(".git").is_dir():
            typer.echo("Installing pre-commit hooks...")
            subprocess.run(["uv", "run", "pre-commit", "install"], check=True)

        typer.echo("Setup complete. Run tasks via 'uv run poe <task>'")
    except subprocess.CalledProcessError as e:
        typer.echo(f"Setup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    setup()
