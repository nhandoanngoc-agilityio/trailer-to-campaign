"""Every CI job has a time limit (T29): without one, a hung test holds a job for GitHub's default of 6 hours.

No YAML parser is a declared dependency, so `job_limits` reads the job level of a workflow by indentation. Anything it
doesn't recognise counts as no limit or raises, so its mistakes fail the test rather than pass it.
"""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# Sized from real numbers: the slowest of the last 31 PR runs of CI took 235 seconds.
# A new job needs a line here, so its limit is a choice someone made.
AGREED_LIMITS = {
    "ci.yml": {"test": 20, "answer-key": 5},
    "claude-code-review.yml": {"claude-review": 30},
    "claude.yml": {"claude": 30},
}

_KEY = re.compile(r"(?P<indent> *)(?P<key>[A-Za-z_][\w-]*):(?:\s+(?P<value>.*))?$")
_COMMENT = re.compile(r"(?:^|\s)#.*$")


def _content_lines(text: str) -> list[str]:
    """The lines that hold YAML, without blanks and whole-line comments."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _limit(value: str | None) -> int | None:
    """A limit is a positive whole number; a blank, an expression, a quoted number or an alias doesn't count."""
    bare = _COMMENT.sub("", value or "").strip()
    return int(bare) if re.fullmatch(r"[1-9]\d*", bare) else None


def job_limits(text: str) -> dict[str, int | None]:
    """Each job in a workflow, with its job-level `timeout-minutes`, or None if it has no usable one.

    Only a key exactly at the job's own indent counts. Block text like `run: |` sits deeper than its key, so it can't
    reach that indent, and neither can a limit on a step or under `strategy:`.
    """
    lines = _content_lines(text)
    starts = [i for i, line in enumerate(lines) if _COMMENT.sub("", line).rstrip() == "jobs:"]
    if len(starts) != 1:
        raise ValueError(f"expected one top-level `jobs:` block, found {len(starts)}")

    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if _indent(line) == 0:  # the next top-level key ends the jobs
            break
        body.append(line)
    if not body:
        raise ValueError("`jobs:` has no jobs")

    job_indent = _indent(body[0])
    limits: dict[str, int | None] = {}
    job: str | None = None
    property_indent: int | None = None
    for line in body:
        indent = _indent(line)
        if indent < job_indent:
            raise ValueError(f"can't read this line of `jobs:`: {line!r}")
        if indent == job_indent:
            match = _KEY.fullmatch(line)
            if not match:
                raise ValueError(f"can't read this job: {line!r}")
            job = match["key"]
            limits[job] = None
            property_indent = None
            continue
        if property_indent is None:
            property_indent = indent  # a job's first line sets the indent of its properties
        if indent == property_indent and (match := _KEY.fullmatch(line)) and match["key"] == "timeout-minutes":
            limits[job] = _limit(match["value"])
    return limits


def workflow_files() -> list[Path]:
    return sorted(p for p in WORKFLOWS.iterdir() if p.suffix in {".yml", ".yaml"})


# --- The real workflows ---


def test_every_job_in_every_workflow_has_a_limit():
    missing = [
        f"{path.name}: {job}"
        for path in workflow_files()
        for job, limit in job_limits(path.read_text(encoding="utf-8")).items()
        if limit is None
    ]
    assert not missing, f"these jobs have no timeout-minutes, so a hang holds them for 6 hours: {missing}"


def test_the_limits_are_the_agreed_ones():
    found = {path.name: job_limits(path.read_text(encoding="utf-8")) for path in workflow_files()}
    assert found == AGREED_LIMITS


# --- The check itself: it accepts a job with a limit ---


def test_accepts_a_limit_on_every_job():
    workflow = """\
        name: CI
        on: push
        jobs:
          first:
            runs-on: ubuntu-latest
            timeout-minutes: 20
            steps:
              - run: pytest
          second-job:
            timeout-minutes: 5  # a comment after the value is fine
            runs-on: ubuntu-latest
    """
    assert job_limits(dedent(workflow)) == {"first": 20, "second-job": 5}


def test_accepts_a_limit_after_nested_lines_and_at_another_indent_width():
    workflow = """\
        jobs:
            only:
                # a comment before the first property doesn't set the indent
                if: |
                    github.event_name == 'push'
                steps:
                -   run: pytest
                timeout-minutes: 10
    """
    assert job_limits(dedent(workflow)) == {"only": 10}


def test_a_key_after_the_jobs_does_not_count_as_a_job():
    workflow = """\
        jobs:
          only:
            timeout-minutes: 5
        env:
          SOME_SETTING: 1
    """
    assert job_limits(dedent(workflow)) == {"only": 5}


# --- The check itself: it rejects a job without one ---


@pytest.mark.parametrize(
    "job",
    [
        pytest.param("runs-on: ubuntu-latest", id="no limit"),
        pytest.param("# timeout-minutes: 5\nruns-on: ubuntu-latest", id="whole-line comment"),
        pytest.param("runs-on: ubuntu-latest  # timeout-minutes: 5", id="comment after another key"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: # 5", id="the value is only a comment"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes:", id="blank value"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes:5", id="no space, so not a key"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: 0", id="zero"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: -1", id="negative"),
        pytest.param('runs-on: ubuntu-latest\ntimeout-minutes: "20"', id="quoted"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: ${{ inputs.limit }}", id="expression"),
        pytest.param("runs-on: ubuntu-latest\nsteps:\n  - run: pytest\n    timeout-minutes: 5", id="on a step"),
        pytest.param("runs-on: ubuntu-latest\nsteps:\n  - timeout-minutes: 5\n    run: pytest", id="starts a step"),
        pytest.param("runs-on: ubuntu-latest\nsteps:\n- timeout-minutes: 5\n  run: pytest", id="on an unindented step"),
        pytest.param("runs-on: ubuntu-latest\nstrategy:\n  timeout-minutes: 5", id="under another key"),
        pytest.param("steps:\n  - run: |\n      timeout-minutes: 5\nruns-on: ubuntu-latest", id="inside block text"),
    ],
)
def test_rejects_a_job_whose_limit_is_missing_or_not_real(job: str):
    workflow = "jobs:\n  only:\n" + "".join(f"    {line}\n" for line in job.splitlines())
    assert job_limits(workflow) == {"only": None}


def test_rejects_a_limit_at_the_workflow_level():
    workflow = """\
        timeout-minutes: 5
        jobs:
          only:
            runs-on: ubuntu-latest
    """
    assert job_limits(dedent(workflow)) == {"only": None}


def test_rejects_a_limit_after_the_jobs_block():
    workflow = """\
        jobs:
          only:
            runs-on: ubuntu-latest
        defaults:
          timeout-minutes: 5
    """
    assert job_limits(dedent(workflow)) == {"only": None}


def test_one_jobs_limit_does_not_cover_the_next():
    workflow = """\
        jobs:
          first:
            timeout-minutes: 5
          second:
            runs-on: ubuntu-latest
    """
    assert job_limits(dedent(workflow)) == {"first": 5, "second": None}


def test_a_job_written_on_one_line_counts_as_no_limit():
    assert job_limits("jobs:\n  only: {runs-on: ubuntu-latest, timeout-minutes: 5}\n") == {"only": None}


@pytest.mark.parametrize(
    "workflow",
    [
        pytest.param("name: CI\non: push\n", id="no jobs block"),
        pytest.param("# jobs:\nname: CI\n", id="jobs only in a comment"),
        pytest.param("jobs:\n", id="an empty jobs block"),
        pytest.param("jobs: {only: {timeout-minutes: 5}}\n", id="jobs written on one line"),
        pytest.param("jobs:\n  - only\n", id="a job that isn't a key"),
    ],
)
def test_a_workflow_it_cannot_read_raises_rather_than_passing(workflow: str):
    with pytest.raises(ValueError):
        job_limits(workflow)
