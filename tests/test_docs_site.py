"""The built docs site (#61 to #64): sovereign, Ghost Stack look, reader features.

The seam is the built site. One build of a copy of docs/ is shared by the
tests; a second build seeds a broken link to prove link problems are warnings.
The copy gets a stub for each generated API page that is absent (CI runs pytest
before `poe docs-api`), so the nav never warns for a file the pipeline makes
later. tests/test_docs_api.py builds the real generated pages, strict.
"""

import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
OWN_DOMAIN = "thomaspeoples.com"

# Every page the site produced before the restyle (#61): URL directory or file.
EXPECTED_PAGES = [
    "index.html",
    "404.html",
    "architecture/index.html",
    "bus-platform/index.html",
    "contracts/index.html",
    "GLOSSARY/index.html",
    "operations/index.html",
    "runbook-cutover/index.html",
    "ROADMAP/index.html",
    "api/index.html",
    "dbt/index.html",
    "adr/0001-line-stops-memory-and-batching/index.html",
    "adr/0002-line-stops-final-assembly/index.html",
    "adr/0003-replay-verified-by-construction/index.html",
    "adr/0004-api-container-has-no-deploy-automation/index.html",
    "adr/0005-dbt-v2-duckdb-alpha-migration/index.html",
    "adr/0006-bus-positions-schema-optimisation/index.html",
    "adr/0007-parquet-archive/index.html",
    "adr/0008-published-timetables-vs-inferred-routes/index.html",
    "adr/0009-fast-nightly-build/index.html",
    "adr/0010-eta-from-daily-statistics-and-3-day-hot-window/index.html",
    "adr/0011-contract-first-dbt/index.html",
    "adr/spikes/index.html",
    "learn-dbt/index.html",
    "learn-dbt/the-rules/index.html",
    "learn-dbt/1-take-off/index.html",
    "learn-dbt/2-a-model-is-a-select/index.html",
    "learn-dbt/3-ref-and-the-graph/index.html",
    "learn-dbt/4-tests/index.html",
    "learn-dbt/5-materializations/index.html",
    "learn-dbt/6-jinja-and-macros/index.html",
    "learn-dbt/7-docs-and-lineage/index.html",
    "learn-dbt/landing/index.html",
    "learn-dbt/cheatsheet/index.html",
    "agents/domain/index.html",
    "agents/issue-tracker/index.html",
    "agents/triage-labels/index.html",
]
MERMAID_PAGES = [
    "adr/0001-line-stops-memory-and-batching/index.html",
    "learn-dbt/index.html",
    "learn-dbt/3-ref-and-the-graph/index.html",
]


class _RequestRefs(HTMLParser):
    """Collects every URL an HTML page makes the browser fetch.

    Anchors are navigation, not requests, so outbound <a> links are not
    collected. Inline <style> blocks and style attributes are scanned for
    url() and @import.
    """

    _SRC_ATTRS = {
        "script": ("src",),
        "img": ("src", "srcset"),
        "source": ("src", "srcset"),
        "iframe": ("src",),
        "link": ("href",),
        "video": ("src", "poster"),
        "audio": ("src",),
        "embed": ("src",),
        "object": ("data",),
        "input": ("src",),
    }
    _CSS_URL = re.compile(r"""url\(\s*['"]?([^'")\s]+)|@import\s+['"]([^'"]+)""")

    def __init__(self):
        super().__init__()
        self.refs: list[str] = []
        self._in_style = False

    def _css(self, text):
        self.refs += [a or b for a, b in self._CSS_URL.findall(text)]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for name in self._SRC_ATTRS.get(tag, ()):
            if not a.get(name):
                continue
            if name == "srcset":
                self.refs += [p.split()[0] for p in a[name].split(",") if p.strip()]
            else:
                self.refs.append(a[name])
        if a.get("style"):
            self._css(a["style"])
        self._in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self._css(data)


def third_party_refs(site: Path) -> dict[str, list[str]]:
    """Map page to the absolute (or protocol-relative) hosts it fetches from
    that are not the owner's own domains."""
    found: dict[str, list[str]] = {}
    files = [*site.rglob("*.html"), *site.rglob("*.css")]
    for path in files:
        text = path.read_text(errors="ignore")
        if path.suffix == ".html":
            parser = _RequestRefs()
            parser.feed(text)
            refs = parser.refs
        else:
            refs = [a or b for a, b in _RequestRefs._CSS_URL.findall(text)]
        bad = []
        for ref in refs:
            if not ref.startswith(("http://", "https://", "//")):
                continue
            host = urlparse(ref if ref[0] != "/" else "https:" + ref).hostname or ""
            if host != OWN_DOMAIN and not host.endswith("." + OWN_DOMAIN):
                bad.append(ref)
        if bad:
            found[str(path.relative_to(site))] = bad
    return found


def missing_pages(site: Path, expected: list[str]) -> list[str]:
    return [p for p in expected if not (site / p).is_file()]


def _build(workdir: Path, extra_pages: dict[str, str] | None = None):
    """Build a copy of docs/ with the real mkdocs.yml; return (site, output)."""
    docs = workdir / "docs"
    shutil.copytree(ROOT / "docs", docs)
    for page in re.findall(r"api/\w+\.md", (ROOT / "mkdocs.yml").read_text()):
        if not (docs / page).exists():
            (docs / page).write_text("# Python API\n")
    for name, body in (extra_pages or {}).items():
        (docs / name).write_text(body)
    config = workdir / "mkdocs.yml"
    config.write_text(f"INHERIT: {ROOT / 'mkdocs.yml'}\ndocs_dir: {docs}\n")
    site = workdir / "site"
    proc = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "-f", str(config), "-d", str(site)],
        cwd=ROOT,  # snippet paths in mkdocs.yml are relative to the repo root
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return site, proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("docs_site"))


@pytest.fixture(scope="module")
def site(built):
    return built[0]


# --- the check itself is trustworthy (seeded, so it can be seen red) ---------


def test_scanner_flags_a_seeded_third_party_script_font_and_css(tmp_path):
    (tmp_path / "page.html").write_text(
        '<link rel="preconnect" href="https://fonts.gstatic.com">'
        '<script src="https://unpkg.com/mermaid@11/dist/mermaid.min.js"></script>'
        '<img src="//img.shields.io/badge.svg">'
        "<style>@import 'https://cdn.example.com/a.css'; b{background:url(https://x.test/y.png)}</style>"
    )
    (tmp_path / "a.css").write_text("@font-face{src:url(https://fonts.example.com/f.woff2)}")
    found = third_party_refs(tmp_path)
    assert set(found) == {"page.html", "a.css"}
    assert len(found["page.html"]) == 5


def test_scanner_allows_own_domains_relative_urls_and_plain_links(tmp_path):
    (tmp_path / "page.html").write_text(
        '<link rel="canonical" href="https://www.thomaspeoples.com/x/">'
        '<script src="javascripts/vendor/mermaid.min.js"></script>'
        '<a href="https://github.com/grafana/grafana/issues/1">outbound link, not a request</a>'
    )
    assert third_party_refs(tmp_path) == {}


def test_missing_page_check_flags_a_seeded_missing_page(tmp_path):
    (tmp_path / "index.html").write_text("x")
    assert missing_pages(tmp_path, ["index.html", "gone/index.html"]) == ["gone/index.html"]


# --- #62 sovereign -----------------------------------------------------------


def test_built_site_makes_no_third_party_request(site):
    assert third_party_refs(site) == {}


def test_mermaid_is_vendored_with_a_source_note():
    vendor = ROOT / "docs" / "javascripts" / "vendor"
    assert (vendor / "mermaid.min.js").stat().st_size > 1_000_000
    note = (vendor / "SOURCE.txt").read_text()
    assert re.search(r"Mermaid \d+\.\d+\.\d+", note) and "update" in note.lower()


@pytest.mark.parametrize("page", MERMAID_PAGES)
def test_mermaid_pages_load_the_vendored_copy_synchronously(site, page):
    html = (site / page).read_text()
    assert 'class="mermaid"' in html
    # Material renders diagrams on DOMContentLoaded; a plain (not async/defer)
    # script in the body has run by then, so `mermaid` is defined and Material
    # never fetches its CDN copy.
    tag = re.search(r'<script src="[^"]*javascripts/vendor/mermaid\.min\.js"[^>]*>', html)
    assert tag and "async" not in tag.group(0) and "defer" not in tag.group(0)
    assert (site / "javascripts/vendor/mermaid.min.js").is_file()


def test_build_does_not_download_a_second_copy_of_mermaid(site):
    assert not list(site.glob("assets/external/**/mermaid*"))


def test_privacy_cache_is_gitignored():
    ignored = subprocess.run(
        ["git", "check-ignore", ".cache/plugin/privacy/x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ".cache/plugin/privacy/x" in ignored


# --- #63 look and feel -------------------------------------------------------


def test_every_existing_page_is_still_produced(site):
    assert missing_pages(site, EXPECTED_PAGES) == []


def test_home_page_declares_dark_scheme_first_with_a_light_toggle(site):
    html = (site / "index.html").read_text()
    schemes = re.findall(r'data-md-color-scheme="([^"]+)"', html)
    # <body> carries the active scheme, then the palette inputs list slate before default.
    assert schemes[0] == "slate"
    assert schemes.index("slate") < schemes.index("default")
    assert re.search(r'<body[^>]*data-md-color-scheme="slate"', html)


def test_site_uses_system_fonts_and_the_ghost_stack_identity(site):
    html = (site / "index.html").read_text()
    assert "fonts.googleapis.com" not in html
    assert "assets/ghost.svg" in html
    assert "assets/favicon.svg" in html
    assert "Sovereign. Self-hosted. No nonsense." in html
    assert "stylesheets/extra.css" in html
    assert "md-tabs" in html and "md-footer" in html


def test_social_links_point_home_and_to_gitea(site):
    html = (site / "index.html").read_text()
    assert 'href="https://www.thomaspeoples.com/"' in html
    assert "https://git.thomaspeoples.com/thomaspeoples/wraith-bus-ingestor/issues" in html


def test_nav_order_is_unchanged(site):
    html = (site / "index.html").read_text()
    tabs = re.search(r'<nav class="md-tabs".*?</nav>', html, re.S).group(0)
    labels = [
        re.sub(r"\s+", " ", t).strip()
        for t in re.findall(r"md-tabs__link[^>]*>(.*?)</a>", tabs, re.S)
    ]
    assert labels == [
        "Home",
        "Architecture",
        "Bus Platform",
        "Contracts",
        "Glossary",
        "Operations",
        "Cutover runbook",
        "Roadmap",
        "Architecture Decisions",
        "Python API",
        "dbt Docs",
        "Learn dbt",
    ]


# --- #64 reader features -----------------------------------------------------


def _index(site):
    return json.loads((site / "search" / "search_index.json").read_text())


def _tokens(index, text):
    # Material splits on the separator before lower-casing, so camelCase works.
    text = re.sub(r"<[^>]+>", " ", text)  # index text is HTML; the client strips tags
    return {t.lower() for t in re.split(index["config"]["separator"], text) if t}


def _pages_mentioning(idx, name):
    return [d for d in idx["docs"] if name in d["text"] or name in d["title"]]


def test_search_finds_snake_case_names_by_a_fragment(site):
    idx = _index(site)
    pages = _pages_mentioning(idx, "bus_positions")
    assert pages
    for d in pages:
        assert {"bus", "positions"} <= _tokens(idx, d["text"] + " " + d["title"]), d["location"]


def test_search_finds_camel_case_names_by_a_fragment(site):
    idx = _index(site)
    pages = _pages_mentioning(idx, "physicalType")
    assert pages
    for d in pages:
        assert {"physical", "type"} <= _tokens(idx, d["text"]), d["location"]


def test_search_separator_splits_dotted_names_but_keeps_version_numbers(site):
    sep = _index(site)["config"]["separator"]
    assert [t for t in re.split(sep, "servers.type") if t] == ["servers", "type"]
    assert [t for t in re.split(sep, "v3.2.0") if t] == ["v3.2.0"]


def test_glossary_terms_have_hover_definitions(site):
    html = (site / "index.html").read_text() + (site / "architecture" / "index.html").read_text()
    assert "<abbr" in html
    # The glossary itself defines the term, so its page carries the hover too.
    assert "<abbr" in (site / "GLOSSARY" / "index.html").read_text()


# "Run" collides with the verb and with "Run 1" / "Run 2" in the runbooks.
HOVER_EXEMPT = {"Run"}


def _glossary_terms():
    text = (ROOT / "docs" / "GLOSSARY.md").read_text()
    return re.findall(r"^\*\*(.+?)\*\*:$", text, re.M)


def test_every_glossary_term_and_acronym_is_in_the_abbreviations_snippet():
    snippet = (ROOT / "docs" / "assets" / "abbreviations.md").read_text()
    defined = set(re.findall(r"^\*\[(.+?)\]:", snippet, re.M))
    expected = set()
    for term in _glossary_terms():
        if term in HOVER_EXEMPT:
            continue
        m = re.fullmatch(r"(.+?) \(([A-Z]+)\)", term)
        expected |= {m.group(1), m.group(2)} if m else {term}
    assert not expected - defined, f"glossary terms without a hover: {sorted(expected - defined)}"


def test_abbreviations_snippet_is_not_a_page(site):
    assert not (site / "assets" / "abbreviations").exists()
    assert not (site / "assets" / "abbreviations.md").exists()


def test_agent_notes_are_published_but_out_of_the_nav(site):
    assert (site / "agents" / "issue-tracker" / "index.html").is_file()
    nav = re.search(
        r'<nav class="md-nav md-nav--primary[^"]*".*?</nav>',
        (site / "index.html").read_text(),
        re.S,
    ).group(0)
    assert "agents/" not in nav


def test_real_build_has_no_warnings(built):
    warnings = [line for line in built[1].splitlines() if line.startswith("WARNING")]
    assert warnings == []


def test_a_broken_link_and_a_broken_anchor_are_build_warnings(tmp_path):
    _, out = _build(
        tmp_path,
        {"seeded.md": "# Seeded\n\n[gone](nowhere.md) and [anchor](index.md#no-such-heading)\n"},
    )
    warnings = [line for line in out.splitlines() if line.startswith("WARNING")]
    assert any("nowhere.md" in w for w in warnings)
    assert any("no-such-heading" in w for w in warnings)
