"""Every CI job has a time limit (T29): without one, a hung test holds a job for GitHub's default of 6 hours.

No YAML parser is a declared dependency, so `job_limits` reads the job level of a workflow by indentation. It isn't a
parser and doesn't understand everything YAML allows. It refuses the shapes known to mislead it: a job's key line with a
value, a value left open by `{`, `[` or a quote, a line at column 0 that isn't a key, and a job with `uses:`. That also
refuses some valid YAML, such as a list written over several lines, and the error says what to change. A few unusual
shapes can still fool it; docs/tickets/T29.md names them.
"""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# Sized from real numbers. Measured per job on 2026-11-05, over 120 PR runs of CI since 2026-10-29: the slowest passing
# `test` took 240 seconds and `answer-key` 10. A job's time doesn't include its wait in the queue.
# A new job needs a line here, so its limit is a choice someone made.
AGREED_LIMITS = {
    "ci.yml": {"test": 20, "answer-key": 5},
    "claude-code-review.yml": {"claude-review": 30},
    "claude.yml": {"claude": 30},
}

_KEY = re.compile(
    r"""(?P<indent> *)(?P<quote>["']?)(?P<key>[A-Za-z_][\w-]*)(?P=quote):(?:\s+(?P<value>.*))?$""", re.ASCII
)
_COMMENT = re.compile(r"(?:^|\s)#.*$", re.ASCII)
# A `key: value` or `- value` line whose value starts with a quote, `{` or `[`, past any anchor or tag.
_OPENING_VALUE = re.compile(r""" *(?:(?:- +)+(?:[^\s:]+: +)?|[^\s:]+: +)(?:[&!]\S* +)*(?P<value>["'\[{].*)""", re.ASCII)
# A whole quoted string, a quote nothing closes, a bracket, or a comment, which hides any bracket inside it.
_FLOW_TOKEN = re.compile(r""""(?:[^"\\]|\\.)*"|'(?:[^']|'')*'(?!')|["'\[\]{}]|\s#.*""", re.ASCII)


def _content_lines(text: str) -> list[str]:
    """The lines that hold YAML, without blanks and whole-line comments. Only `\\n` ends a line: `splitlines()` also
    splits on U+2028, a form feed and others, which could turn part of a comment into a line at column 0."""
    return [line for line in text.split("\n") if line.strip() and not line.lstrip().startswith("#")]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _limit(value: str | None) -> int | None:
    """A limit is a positive whole number; a blank, an expression, a quoted number or an alias doesn't count."""
    bare = _COMMENT.sub("", value or "").strip(" \t\r")
    return int(bare) if re.fullmatch(r"[1-9][0-9]*", bare) else None


def _leaves_open(line: str) -> bool:
    """Whether a value starts a `{`, `[` or quote that this line doesn't close, so later lines only look like keys."""
    match = _OPENING_VALUE.fullmatch(line)
    if not match:
        return False
    depth = 0
    for token in _FLOW_TOKEN.findall(match["value"]):
        if token in {'"', "'"}:
            return True
        depth += {"[": 1, "{": 1, "]": -1, "}": -1}.get(token, 0)
        if depth <= 0:
            return False
    return True


def job_limits(text: str) -> dict[str, int | None]:
    """Each job in a workflow, with its job-level `timeout-minutes`, or None if it has no usable one.

    Only a key exactly at the job's own indent counts. Block text like `run: |` sits deeper than its key, so it can't
    reach that indent, and neither can a limit on a step or under `strategy:`.
    """
    lines = _content_lines(text)
    for line in lines:
        if _leaves_open(line):
            raise ValueError(f"this value carries on to the next line, so write it on one line: {line!r}")
    starts = [i for i, line in enumerate(lines) if _COMMENT.sub("", line).rstrip() == "jobs:"]
    if len(starts) != 1:
        raise ValueError(f"expected one top-level `jobs:` block, found {len(starts)}")

    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if _indent(line) == 0:  # the next top-level key ends the jobs
            if not _KEY.fullmatch(line):
                raise ValueError(f"can't read this line at column 0 of `jobs:`: {line!r}")
            break
        body.append(line)
    if not body:
        raise ValueError("`jobs:` has no jobs")

    job_indent = _indent(body[0])
    limits: dict[str, int | None] = {}
    job: str | None = None
    property_indent: int | None = None
    for position, line in enumerate(body):
        indent = _indent(line)
        if indent < job_indent:
            raise ValueError(f"can't read this line of `jobs:`: {line!r}")
        if indent == job_indent:
            match = _KEY.fullmatch(line)
            if not match:
                raise ValueError(f"can't read this job: {line!r}")
            if not re.fullmatch(r"(?:&\S+)?", _COMMENT.sub("", match["value"] or "").strip(" \t\r")):
                raise ValueError(f"a job's key line can hold only a comment or an anchor: {line!r}")
            job = match["key"]
            limits[job] = None
            property_indent = None
            continue
        if property_indent is None:
            property_indent = indent  # a job's first line sets the indent of its properties
        if indent == property_indent and (match := _KEY.fullmatch(line)):
            if match["key"] == "uses":
                raise ValueError(
                    f"job {job!r} calls a reusable workflow. GitHub doesn't list `timeout-minutes` among the keywords "
                    "such a job can use, so a person must decide how to limit it."
                )
            if match["key"] == "timeout-minutes":
                carries_on = position + 1 < len(body) and _indent(body[position + 1]) > indent  # `5` then `6` is "5 6"
                limits[job] = None if carries_on else _limit(match["value"])
    return limits


def workflow_files(directory: Path = WORKFLOWS) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.suffix in {".yml", ".yaml"})


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
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: 1٥", id="a non-ASCII digit"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: 5", id="a no-break space, so not a key"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: 5 ", id="a no-break space in the value"),
        pytest.param("runs-on: ubuntu-latest\ntimeout-minutes: 5\n  6", id="the value carries on to a deeper line"),
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


def test_a_later_job_stays_visible_after_a_line_separator_or_form_feed_in_a_comment():
    for character in "\u2028\u2029\u0085\x0c\x0b":
        workflow = f"jobs:\n  a:\n    timeout-minutes: 5\n    # note{character}more\n  b:\n    runs-on: x\n"
        assert job_limits(workflow) == {"a": 5, "b": None}, repr(character)


def test_each_job_measures_its_own_property_indent():
    deeper_then_shallower = """\
        jobs:
          a:
              timeout-minutes: 5
          b:
            strategy:
              timeout-minutes: 7
    """
    shallower_then_deeper = """\
        jobs:
          a:
            timeout-minutes: 5
          b:
              timeout-minutes: 7
    """
    assert job_limits(dedent(deeper_then_shallower)) == {"a": 5, "b": None}
    assert job_limits(dedent(shallower_then_deeper)) == {"a": 5, "b": 7}


@pytest.mark.parametrize(
    "workflow",
    [
        pytest.param("name: CI\non: push\n", id="no jobs block"),
        pytest.param("# jobs:\nname: CI\n", id="jobs only in a comment"),
        pytest.param("jobs:\n", id="an empty jobs block"),
        pytest.param("jobs: {only: {timeout-minutes: 5}}\n", id="jobs written on one line"),
        pytest.param("jobs:\n  - only\n", id="a job that isn't a key"),
        pytest.param("jobs:\n  a:\n    timeout-minutes: 5\njobs:\n  b:\n    runs-on: x\n", id="two jobs blocks"),
        # A job's key line can't carry a value: its limit would be somewhere this check doesn't read.
        pytest.param("jobs:\n  only: {runs-on: ubuntu-latest, timeout-minutes: 5}\n", id="a job on one line"),
        pytest.param(
            "jobs:\n  a: {\n    env: {\n    timeout-minutes: 5\n    }, runs-on: x\n    }\n", id="a job as open {"
        ),
        pytest.param("jobs:\n  a: |\n    timeout-minutes: 5\n", id="a job as block text"),
        pytest.param("jobs:\n  a: >-\n    timeout-minutes: 5\n", id="a job as folded text"),
        pytest.param("jobs:\n  a: *shared\n  b:\n    timeout-minutes: 5\n", id="a job as an alias"),
        # A value left open carries on to later lines, which only look like keys.
        pytest.param("jobs:\n  a:\n    env: {\n    timeout-minutes: 5\n    }\n", id="an open {"),
        pytest.param("jobs:\n  a:\n    needs: [\n    timeout-minutes: 5\n    ]\n", id="an open ["),
        pytest.param('jobs:\n  a:\n    name: "foo\n    timeout-minutes: 5\n    bar"\n', id="an open double quote"),
        pytest.param("jobs:\n  a:\n    name: 'foo\n    timeout-minutes: 5\n    bar'\n", id="an open single quote"),
        pytest.param("jobs:\n  a:\n    env: &shared {\n    timeout-minutes: 5\n    }\n", id="open after an anchor"),
        pytest.param('jobs:\n  a:\n    name: "foo\\"\n    timeout-minutes: 5\n    bar"\n', id="an escaped quote"),
        pytest.param("jobs:\n  a:\n    name: 'it''s\n    timeout-minutes: 5\n    bar'\n", id="a doubled quote"),
        pytest.param(
            'jobs:\n  a:\n    steps:\n      - run: "foo\n    timeout-minutes: 5\n      bar"\n', id="open on a step"
        ),
        pytest.param("jobs:\n  a:\n    args:\n      - {\n    timeout-minutes: 5\n      }\n", id="open list item"),
        pytest.param("jobs:\n  a:\n    env: { # }\n    timeout-minutes: 5\n    }\n", id="a comment hides the }"),
        pytest.param(
            'name: "foo\njobs:\n  a:\n    timeout-minutes: 5\nbar"\n', id="open above jobs, hiding the whole block"
        ),
        # A line at column 0 must really end the jobs, or the jobs after it are never seen.
        pytest.param('jobs:\n  a:\n    name: "x\nz: y"\n  b:\n    runs-on: x\n', id="column 0 in a quoted value"),
        pytest.param(
            'jobs:\n  a:\n    steps:\n      - run: "x\nz: y"\n  b:\n    runs-on: x\n', id="column 0 in a step's value"
        ),
        pytest.param("jobs:\n  a:\n    runs-on: x\nnot a key\n  b:\n    runs-on: x\n", id="column 0 and not a key"),
        pytest.param("jobs:\n  a:\n    runs-on: x\n\tz\n  b:\n    runs-on: x\n", id="a tab at column 0"),
    ],
)
def test_a_workflow_it_cannot_read_raises_rather_than_passing(workflow: str):
    with pytest.raises(ValueError):
        job_limits(workflow)


# --- The check itself: a job that calls a reusable workflow is a person's decision ---


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param("uses: ./.github/workflows/shared.yml", id="no limit"),
        pytest.param("uses: ./.github/workflows/shared.yml\ntimeout-minutes: 5", id="limit after"),
        pytest.param("timeout-minutes: 5\nuses: ./.github/workflows/shared.yml", id="limit before"),
        pytest.param('"uses": ./.github/workflows/shared.yml\ntimeout-minutes: 5', id="a quoted key"),
    ],
)
def test_a_job_that_calls_a_reusable_workflow_is_refused_whatever_it_sets(properties: str):
    workflow = "jobs:\n  shared:\n" + "".join(f"    {line}\n" for line in properties.splitlines())
    with pytest.raises(ValueError, match="(?s)shared.*reusable workflow.*person must decide"):
        job_limits(workflow)


def test_a_step_that_uses_an_action_is_not_a_reusable_workflow():
    workflow = """\
        jobs:
          only:
            timeout-minutes: 5
            steps:
              - uses: actions/checkout@v7
              - name: another
                uses: actions/setup-python@v5
    """
    assert job_limits(dedent(workflow)) == {"only": 5}


# --- The check itself: values that close on their own line, or look open but aren't, still pass ---


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('name: "foo # bar"', id="a hash inside quotes"),
        pytest.param('name: "foo"  # it\'s fine', id="a quote in a comment after the value"),
        pytest.param('name: "say \\"hi\\""', id="escaped quotes that close"),
        pytest.param("name: 'it''s closed'", id="doubled quotes that close"),
        pytest.param("name: Don't panic", id="an apostrophe in a plain value"),
        pytest.param('run: echo "hi', id="a quote inside a plain value"),
        pytest.param("if: github.event_name == 'pull_request'", id="quotes inside a plain value"),
        pytest.param("env: {A: 1, B: [2, 3]}", id="flow that closes"),
        pytest.param("needs: [a, 'b']  # it's fine", id="a list that closes, then a comment"),
        pytest.param("env: &shared {A: 1}", id="an anchor before flow that closes"),
        pytest.param("env: ${{ fromJSON('{}') }}", id="an expression"),
        pytest.param("if: |\n  { echo open\n  [ -n x", id="block text that looks open"),
    ],
)
def test_a_value_that_is_closed_is_not_refused(line: str):
    properties = "".join(f"    {part}\n" for part in line.splitlines())
    assert job_limits(f"jobs:\n  only:\n{properties}    timeout-minutes: 5\n") == {"only": 5}


def test_a_job_key_line_may_hold_a_comment_or_an_anchor():
    workflow = """\
        jobs:
          first: # a comment
            timeout-minutes: 5
          second: &shared
            timeout-minutes: 7
          third: &other # both
            timeout-minutes: 9
    """
    assert job_limits(dedent(workflow)) == {"first": 5, "second": 7, "third": 9}


def test_block_text_ends_where_the_next_property_starts():
    """A limit at the property's own indent, straight after `|`, is a real key: YAML ends the block text there."""
    assert job_limits("jobs:\n  a:\n    if: |\n    timeout-minutes: 5\n") == {"a": 5}
    assert job_limits("jobs:\n  a:\n    if: >-\n      text\n    timeout-minutes: 5\n") == {"a": 5}


def test_block_text_deeper_than_its_property_hides_no_limit():
    for indicator in ["|", ">", "|-", ">+", "|2", ">1-"]:
        workflow = f"jobs:\n  a:\n    if: {indicator}\n      timeout-minutes: 5\n"
        assert job_limits(workflow) == {"a": None}, indicator


def test_a_comment_after_a_limit_does_not_carry_its_value_on():
    assert job_limits("jobs:\n  a:\n    timeout-minutes: 5\n      # a deeper note\n    runs-on: x\n") == {"a": 5}


def test_a_quoted_timeout_key_is_still_the_key():
    assert job_limits('jobs:\n  a:\n    "timeout-minutes": 5\n') == {"a": 5}


def test_windows_line_endings_still_read():
    assert job_limits("jobs:\r\n  a: # note\r\n    timeout-minutes: 5\r\n  b:\r\n    runs-on: x\r\nenv:\r\n") == {
        "a": 5,
        "b": None,
    }


# --- Which files count ---


def test_both_yaml_spellings_are_read_and_nothing_else(tmp_path: Path):
    for name in ["one.yml", "two.yaml", "notes.md", "yml"]:
        (tmp_path / name).write_text("", encoding="utf-8")
    assert [path.name for path in workflow_files(tmp_path)] == ["one.yml", "two.yaml"]
