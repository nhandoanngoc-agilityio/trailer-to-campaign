"""The review-checklist skill names real files, so it can't go stale silently (T25)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "skills" / "review-checklist" / "SKILL.md"


def named_paths(text: str) -> set[str]:
    """Backticked words that look like repo paths; a placeholder (`T<n>`) or a command (`/code-review`) is left out."""
    words = [w for w in re.findall(r"`([^`\s]+)`", text) if not w.startswith("/") and not set(w) & set("<>*$")]
    return {w.rstrip("/") for w in words if "/" in w or w.endswith((".py", ".md", ".toml", ".lock", ".yml", ".json"))}


def test_named_paths_finds_paths_and_leaves_out_the_rest() -> None:
    text = (
        "See `src/a.py`, `docs/`, `CONTEXT.md`, `uv.lock`, not `/code-review`, `T<n>.md`, `git diff` or `MAX_RETRIES`."
    )
    assert named_paths(text) == {"src/a.py", "docs", "CONTEXT.md", "uv.lock"}


def test_the_skill_has_the_frontmatter_claude_code_needs() -> None:
    frontmatter = SKILL.read_text().split("---")[1]
    assert "name: review-checklist" in frontmatter
    assert "description:" in frontmatter


def test_every_path_the_skill_names_exists() -> None:
    paths = named_paths(SKILL.read_text())
    missing = sorted(p for p in paths if not (ROOT / p).exists())
    assert not missing, f"the skill names paths that don't exist: {missing}"


def test_the_retry_cap_the_skill_names_exists() -> None:
    assert "MAX_RETRIES" in SKILL.read_text()
    assert "MAX_RETRIES = " in (ROOT / "src" / "trailer_to_campaign" / "agents" / "review.py").read_text()


def test_every_ticket_the_skill_cites_has_a_lesson() -> None:
    cited = set(re.findall(r"\bT\d+\b", SKILL.read_text()))
    assert cited
    lessons = (ROOT / "docs" / "lessons.md").read_text()
    missing = sorted(t for t in cited if f"| {t} |" not in lessons)
    assert not missing, f"the skill cites tickets with no lesson: {missing}"


def test_every_adr_the_skill_cites_exists() -> None:
    cited = set(re.findall(r"ADR (\d{4})", SKILL.read_text()))
    assert cited
    missing = sorted(a for a in cited if not list((ROOT / "docs" / "adr").glob(f"{a}-*.md")))
    assert not missing, f"the skill cites ADRs that don't exist: {missing}"
