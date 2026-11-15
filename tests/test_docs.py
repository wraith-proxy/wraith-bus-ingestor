"""Docs hygiene: every ADR is in the mkdocs nav, and no live file cites a
CONTRACT.md that does not exist."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_adr_is_in_the_mkdocs_nav():
    nav = (ROOT / "mkdocs.yml").read_text()
    missing = [
        p.name for p in sorted((ROOT / "docs" / "adr").glob("*.md")) if f"adr/{p.name}" not in nav
    ]
    assert not missing, f"ADRs missing from mkdocs.yml nav: {missing}"


def test_no_dangling_contract_md_reference():
    assert not (ROOT / "CONTRACT.md").exists()
    out = subprocess.run(
        ["git", "grep", "-l", "CONTRACT.md", "--", "dbt/macros", "dbt/models", "gtfs",
         "contracts"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert not out, f"files cite the nonexistent CONTRACT.md: {out}"


def test_dbt_readme_does_not_deny_the_packages_file():
    assert (ROOT / "dbt" / "packages.yml").exists()
    assert "There is no `packages.yml`" not in (ROOT / "dbt" / "README.md").read_text()
