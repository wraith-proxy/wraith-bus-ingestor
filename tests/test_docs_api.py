"""The Python API reference (#60): one generated page per area, built strict.

The seam is `poe docs-api` (pydoc-markdown over every public module, grouped by area)
and the mkdocs build. The pages are generated, never committed. These tests assert
structure (a page per area, in the nav, ignored by git, strict build, private helpers
hidden, a broken reference fails), not docstring wording.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "docs" / "api"

# area page -> nav label
AREAS = {
    "backend": "Backend",
    "ops": "Ops",
    "contracts": "Contracts",
    "gtfs": "GTFS",
    "archiver": "Archiver",
    "ingestor": "Ingestor",
    "dbt": "dbt scripts",
    "tooling": "Tooling",
}


class _NavLoader(yaml.SafeLoader):
    """mkdocs.yml carries python tags (mermaid fence); read them as plain None."""


_NavLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def _nav():
    return yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_NavLoader)["nav"]


def _env():
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def _poe(task, extra_env=None):
    env = _env() | (extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "poethepoet", task],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def generated():
    proc = _poe("docs-api")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return API_DIR


# --- every area has a page, in the nav ---------------------------------------


def _python_api_section():
    for item in _nav():
        if isinstance(item, dict) and "Python API" in item:
            return item["Python API"]
    raise AssertionError("no Python API section in the nav")


def test_python_api_is_a_nav_section_not_the_old_single_page():
    section = _python_api_section()
    assert isinstance(section, list), "Python API must be a section of pages"
    files = [f for entry in section for f in entry.values()]
    assert "api.md" not in files
    assert "api/index.md" in files


def test_every_area_page_is_in_the_python_api_nav():
    section = _python_api_section()
    nav = {label: file for entry in section for label, file in entry.items()}
    for area, label in AREAS.items():
        assert nav.get(label) == f"api/{area}.md", f"{label} missing from the Python API nav"


def _public_areas():
    files = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    areas = set()
    for f in files:
        p = Path(f)
        if p.name.startswith("test_") or "tests" in p.parts or "dbt_packages" in p.parts:
            continue
        areas.add(p.parts[0] if len(p.parts) > 1 else "tooling")
    return areas


def test_every_area_of_public_python_in_the_repo_has_a_page():
    missing = _public_areas() - set(AREAS)
    assert not missing, f"areas with public Python but no API page: {sorted(missing)}"


def test_every_public_module_is_in_the_api_config():
    config = yaml.safe_load((ROOT / "pydoc-markdown.yml").read_text())
    listed = {
        (Path(loader["search_path"][0]) / f"{m}.py").as_posix()
        for loader in config["loaders"]
        for m in loader["modules"]
    }
    files = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    public = {
        f
        for f in files
        if not Path(f).name.startswith("test_")
        and "tests" not in Path(f).parts
        and "dbt_packages" not in Path(f).parts
    }
    missing = public - listed
    assert not missing, f"public modules missing from pydoc-markdown.yml: {missing}"
    assert not listed - public, f"pydoc-markdown.yml lists untracked modules: {listed - public}"


# --- generated and committed -------------------------------------------------


def test_generated_pages_are_committed_not_ignored():
    """The nav must work without a build, so every page is tracked; CI's drift step
    (test.yml) keeps them equal to a fresh `poe docs-api`."""
    for area in ["index", *AREAS]:
        page = f"docs/api/{area}.md"
        ignored = subprocess.run(["git", "check-ignore", "-q", page], cwd=ROOT).returncode
        assert ignored != 0, f"{page} must not be gitignored"
        assert (ROOT / page).is_file(), f"{page} missing; run poe docs-api and commit it"


# --- generation --------------------------------------------------------------


def test_docs_api_generates_a_non_empty_page_per_area(generated):
    for area in AREAS:
        page = generated / f"{area}.md"
        assert page.is_file(), f"{area} page not generated"
        assert len(page.read_text().strip().splitlines()) > 1, f"{area} page is empty"


def test_private_helpers_are_hidden_by_default(generated):
    config = yaml.safe_load((ROOT / "pydoc-markdown.yml").read_text())
    flt = next(p for p in config["processors"] if p["type"] == "filter")
    assert flt.get("exclude_private", True) is True
    for area in AREAS:
        for line in (generated / f"{area}.md").read_text().splitlines():
            assert not re.match(r"#+ \\?_[A-Za-z]", line), f"private helper rendered: {line}"


def _broken_config(tmp_path, modules, contents):
    config = tmp_path / "broken.yml"
    config.write_text(
        f"""
loaders:
  - type: python
    search_path: [{ROOT / "ops"}]
    modules: {modules}
processors:
  - type: filter
renderer:
  type: mkdocs
  output_directory: build/api-docs
  content_directory_name: api
  pages:
    - title: Ops
      name: ops
      contents: {contents}
"""
    )
    return config


def test_docs_api_fails_when_a_referenced_module_cannot_be_found(tmp_path):
    config = _broken_config(tmp_path, "[no_such_module]", "['no_such_module', 'no_such_module.*']")
    proc = _poe("docs-api", {"PYDOC_CONFIG": str(config)})
    assert proc.returncode != 0


def test_docs_api_fails_when_an_area_page_would_be_empty(tmp_path):
    config = _broken_config(tmp_path, "[sla_check]", "['nothing_matches.*']")
    proc = _poe("docs-api", {"PYDOC_CONFIG": str(config)})
    assert proc.returncode != 0


# --- the built site ----------------------------------------------------------


@pytest.fixture(scope="module")
def strict_site(generated, tmp_path_factory):
    work = tmp_path_factory.mktemp("api_site")
    docs = work / "docs"
    shutil.copytree(ROOT / "docs", docs)
    config = work / "mkdocs.yml"
    config.write_text(f"INHERIT: {ROOT / 'mkdocs.yml'}\ndocs_dir: {docs}\n")
    site = work / "site"
    proc = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-f", str(config), "-d", str(site)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return site


def test_docs_build_is_strict_clean_and_produces_every_area_page(strict_site):
    for area in AREAS:
        assert (strict_site / "api" / area / "index.html").is_file(), f"{area} page not built"


def test_old_api_url_still_resolves(strict_site):
    assert (strict_site / "api" / "index.html").is_file()


def test_docs_build_fails_strict_when_an_area_page_is_missing(tmp_path):
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "docs", docs)
    shutil.rmtree(docs / "api", ignore_errors=True)
    (docs / "api").mkdir()
    (docs / "api" / "index.md").write_text("# Python API\n")
    config = tmp_path / "mkdocs.yml"
    config.write_text(f"INHERIT: {ROOT / 'mkdocs.yml'}\ndocs_dir: {docs}\n")
    proc = subprocess.run(
        [
            sys.executable, "-m", "mkdocs", "build", "--strict",
            "-f", str(config), "-d", str(tmp_path / "site"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
