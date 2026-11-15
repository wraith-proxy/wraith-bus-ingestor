"""Nothing about agents or skills is ever committed (owner rule).

AGENTS.md, CLAUDE.md, .claude/, docs/agents/, .scratch/ and skill folders live on the
owner's machine only (see .git/info/exclude). This fails if one is tracked, and the docs
site must never publish a docs/agents/ folder even if one exists locally.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(
    r"(^|/)(AGENTS\.md|CLAUDE\.md|GEMINI\.md|\.claude|\.cursor|\.scratch|skills)(/|$)"
    r"|^docs/agents(/|$)"
)


def test_no_agent_or_skill_files_are_tracked():
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    bad = [f for f in files if FORBIDDEN.search(f)]
    assert not bad, f"agent/skill files must never be committed: {bad}"


def test_docs_site_never_publishes_agent_notes():
    assert "/agents/" in (ROOT / "mkdocs.yml").read_text()
