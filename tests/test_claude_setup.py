"""Claude Code's setup for this repo (T31): CLAUDE.md stays short without losing a rule.

The ticket loop lives in a skill, rules that matter in one folder load only there, and the
reviewer subagent can't edit. These tests keep each move honest.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

import pytest
from test_review_checklist import named_paths

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
TICKET_LOOP = ROOT / ".claude" / "skills" / "ticket-loop" / "SKILL.md"
RULES = ROOT / ".claude" / "rules"
REVIEWER = ROOT / ".claude" / "agents" / "reviewer.md"

# CLAUDE.md's "Never do" lines as they stood on main before T31. Shortening the file must not drop one.
NEVER_DO = [
    "Never commit or push directly to `main`; everything goes through a PR.",
    "Never post a Campaign Draft or record an Approval without a marketer's action in Slack.",
    "Never edit `evals/expected/` to make an Eval Set run pass.",
    "Never send screeners to a model in slice 1 (ADR 0005).",
    "Never use `git push --force` or `git reset --hard`.",
    "Never add a dependency without asking first.",
]


def frontmatter(path: Path) -> dict[str, str]:
    """The YAML frontmatter's top-level keys, each with its raw value (list items joined by newlines)."""
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    block = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    key = ""
    for line in block.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        elif key and line.strip().startswith("- "):
            fields[key] = (fields[key] + "\n" + line.strip()[2:].strip().strip("\"'")).strip()
    return fields


def tracked_files() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()


GITHUB_REPO = "nhandoanngoc-agilityio/trailer-to-campaign"  # named in CLAUDE.md, but not a path


def missing_paths(text: str) -> list[str]:
    """Named paths that exist nowhere. A bare file name (`loop.py`) counts when some file in the repo has that name."""
    names = {Path(f).name for f in tracked_files()}
    return sorted(
        p
        for p in named_paths(text)
        if p != GITHUB_REPO and not (ROOT / p).exists() and not ("/" not in p and p in names)
    )


def test_missing_paths_catches_a_typo_but_not_a_bare_file_name() -> None:
    assert missing_paths("`loop.py`, `CLAUDE.md`, `src/` and `nhandoanngoc-agilityio/trailer-to-campaign`") == []
    assert missing_paths("`lop.py` and `src/nowhere.py`") == ["lop.py", "src/nowhere.py"]


# --- CLAUDE.md ---------------------------------------------------------------------


@pytest.mark.parametrize("line", NEVER_DO)
def test_every_never_do_line_is_still_in_claude_md(line: str) -> None:
    assert line in CLAUDE_MD.read_text()


def test_claude_md_points_to_the_ticket_loop_and_keeps_its_gates() -> None:
    text = CLAUDE_MD.read_text()
    assert "ticket-loop" in text
    assert "🧑" in text and "Plan" in text and "Review" in text
    assert "provenance" in text and "Human review" in text


def test_claude_md_stays_short() -> None:
    assert len(CLAUDE_MD.read_text().splitlines()) < 91  # its length on main before T31


def test_every_path_claude_md_names_exists() -> None:
    missing = missing_paths(CLAUDE_MD.read_text())
    assert not missing, f"CLAUDE.md names paths that don't exist: {missing}"


# --- the ticket-loop skill ------------------------------------------------------------


def test_the_ticket_loop_skill_is_one_claude_loads_by_itself() -> None:
    fields = frontmatter(TICKET_LOOP)
    assert fields.get("name") == "ticket-loop"
    assert "ticket" in fields.get("description", "")
    assert "disable-model-invocation" not in fields


@pytest.mark.parametrize(
    "heading",
    [
        "1. **Analyze.**",
        "2. **Plan** 🧑",
        "3. **Build.**",
        "4. **Verify.**",
        "5. **Ship.**",
        "6. **Review** 🧑",
        "7. **Close the loop.**",
    ],
)
def test_the_ticket_loop_skill_holds_every_step(heading: str) -> None:
    assert heading in TICKET_LOOP.read_text()


@pytest.mark.parametrize(
    "words", ["Pre-PR checks", "t2c eval --only drafts", "--key-moments-from", "A drop in Traps caught blocks the PR"]
)
def test_the_ticket_loop_skill_keeps_the_checks_that_moved_with_it(words: str) -> None:
    assert words in TICKET_LOOP.read_text()


def test_every_path_the_ticket_loop_skill_names_exists() -> None:
    missing = missing_paths(TICKET_LOOP.read_text())
    assert not missing, f"the skill names paths that don't exist: {missing}"


OLD_POINTERS = [
    re.compile(r"CLAUDE\.md, ticket loop step"),
    re.compile(r"ticket loop in `CLAUDE\.md`"),
    re.compile(r"Pre-PR checks\*\* in `CLAUDE\.md`"),
    re.compile(r"CLAUDE\.md#the-ticket-loop"),
]


def test_nothing_points_at_the_ticket_loop_in_claude_md_any_more() -> None:
    """Records and lessons are history, so they may still say where the loop used to be."""
    stale = []
    for name in tracked_files():
        if name.startswith("docs/tickets/") or name == "docs/lessons.md" or name == "tests/test_claude_setup.py":
            continue
        path = ROOT / name
        if path.suffix not in {".md", ".py", ".yml", ".json", ".toml"} or not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        stale += [f"{name}: {p.pattern}" for p in OLD_POINTERS if p.search(text)]
    assert not stale, f"these still point at CLAUDE.md's ticket loop: {stale}"


# --- .claude/rules ---------------------------------------------------------------------


def rule_files() -> list[Path]:
    return sorted(RULES.glob("*.md"))


def test_there_are_rules_for_footage_prompts_the_checker_and_the_budget() -> None:
    assert {p.stem for p in rule_files()} >= {"footage", "prompts", "checker", "budget"}


@pytest.mark.parametrize("rule", rule_files(), ids=lambda p: p.stem)
def test_every_rule_is_scoped_and_each_pattern_matches_a_file(rule: Path) -> None:
    patterns = frontmatter(rule).get("paths", "").split("\n")
    patterns = [p for p in patterns if p]
    assert patterns, f"{rule.name} has no paths: it would load in every session"
    files = tracked_files()
    for pattern in patterns:
        assert any(fnmatch.fnmatch(f, pattern) for f in files), f"{rule.name}: {pattern} matches no tracked file"


@pytest.mark.parametrize("rule", rule_files(), ids=lambda p: p.stem)
def test_every_path_a_rule_names_exists(rule: Path) -> None:
    body = rule.read_text().split("---\n", 2)[2]
    missing = missing_paths(body)
    assert not missing, f"{rule.name} names paths that don't exist: {missing}"


# --- the reviewer subagent -------------------------------------------------------------


def test_the_reviewer_can_read_but_never_edit() -> None:
    fields = frontmatter(REVIEWER)
    assert fields.get("name") == "reviewer"
    tools = {t.strip() for t in fields.get("tools", "").split(",")}
    assert tools == {"Read", "Grep", "Glob"}


# --- .gitignore ------------------------------------------------------------------------


def test_personal_claude_notes_stay_out_of_git() -> None:
    """In the repo's own .gitignore, not a personal global one, so every clone and CI agree."""
    assert "CLAUDE.local.md" in (ROOT / ".gitignore").read_text().splitlines()
