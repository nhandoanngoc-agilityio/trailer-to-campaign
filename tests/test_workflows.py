"""Every CI job has a time limit (T29): without one, a hung test holds a job for GitHub's default of 6 hours."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# Sized from real numbers. Measured per job on 2026-11-05, over PR runs of CI since 2026-10-29: the slowest passing
# `test` took 240 seconds and `answer-key` 10. A job's time doesn't include its wait in the queue.
# A new job needs a line here, so its limit is a choice someone made.
AGREED_LIMITS = {
    "ci.yml: test": 20,
    "ci.yml: answer-key": 5,
    "claude-code-review.yml: claude-review": 30,
    "claude.yml: claude": 30,
}


def read_limits(directory: Path = WORKFLOWS) -> dict[str, int]:
    """Each job's `timeout-minutes`, keyed "file: job". Raises, naming them, if a job has no real limit."""
    files = sorted(p for p in directory.iterdir() if p.suffix in {".yml", ".yaml"})
    if not files:
        raise ValueError(f"no workflow files in {directory}")
    limits = {}
    for path in files:
        with path.open(encoding="utf-8") as file:  # so a YAML error names the file
            workflow = yaml.safe_load(file)
        jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
        if not jobs or not isinstance(jobs, dict):
            raise ValueError(f"{path.name}: no jobs")
        for job, body in jobs.items():
            body = body if isinstance(body, dict) else {}
            if "uses" in body:
                raise ValueError(f"{path.name}: {job}: calls a reusable workflow; a person must decide how to limit it")
            limit = body.get("timeout-minutes")
            if type(limit) is not int or limit < 1:  # `type`, because a YAML `true` is a bool, and a bool is an int
                raise ValueError(f"{path.name}: {job}: set timeout-minutes to a positive whole number, not {limit!r}")
            limits[f"{path.name}: {job}"] = limit
    return limits


def assert_agreed(directory: Path = WORKFLOWS) -> None:
    differing = sorted(read_limits(directory).items() ^ AGREED_LIMITS.items())
    assert not differing, f"these differ from AGREED_LIMITS, so choose a limit on purpose: {differing}"


def write_workflow(directory: Path, text: str, name: str = "w.yml") -> Path:
    (directory / name).write_text(text, encoding="utf-8")
    return directory


def job(*properties: str) -> str:
    return "jobs:\n  only:\n" + "".join(f"    {line}\n" for line in properties)


# --- The real workflows ---


def test_every_job_in_every_workflow_has_its_agreed_limit():
    assert_agreed()


# --- Against the agreed limits, on a copy of the real workflows ---


@pytest.fixture
def workflows(tmp_path: Path) -> Path:
    copy = tmp_path / "workflows"
    shutil.copytree(WORKFLOWS, copy)
    return copy


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_a_copy_of_the_real_workflows_passes(workflows: Path):
    assert_agreed(workflows)


def test_a_new_job_fails_until_someone_picks_its_limit(workflows: Path):
    with (workflows / "ci.yml").open("a", encoding="utf-8") as file:
        file.write("\n  extra:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n")
    with pytest.raises(AssertionError, match="ci.yml: extra"):
        assert_agreed(workflows)


def test_a_new_workflow_file_fails_until_someone_picks_its_limit(workflows: Path):
    write_workflow(workflows, job("timeout-minutes: 5"), "new.yaml")
    with pytest.raises(AssertionError, match="new.yaml: only"):
        assert_agreed(workflows)


def test_a_renamed_job_fails(workflows: Path):
    edit(workflows / "ci.yml", "  answer-key:\n", "  renamed:\n")
    with pytest.raises(AssertionError, match="(?s)ci.yml: answer-key.*ci.yml: renamed"):
        assert_agreed(workflows)


def test_a_changed_limit_fails(workflows: Path):
    edit(workflows / "ci.yml", "timeout-minutes: 20", "timeout-minutes: 21")
    with pytest.raises(AssertionError, match="ci.yml: test"):
        assert_agreed(workflows)


def test_a_removed_workflow_fails(workflows: Path):
    (workflows / "claude.yml").unlink()
    with pytest.raises(AssertionError, match="claude.yml: claude"):
        assert_agreed(workflows)


def test_a_limit_removed_from_a_real_job_fails_and_names_it(workflows: Path):
    edit(workflows / "ci.yml", "    timeout-minutes: 20\n", "")
    with pytest.raises(ValueError, match="ci.yml: test: set timeout-minutes"):
        assert_agreed(workflows)


# --- A valid workflow passes ---


def test_a_limit_on_every_job_is_read(tmp_path: Path):
    text = "jobs:\n  first:\n    runs-on: x\n    timeout-minutes: 20\n  second-job:\n    timeout-minutes: 5  # why\n"
    assert read_limits(write_workflow(tmp_path, text)) == {"w.yml: first": 20, "w.yml: second-job": 5}


def test_an_anchored_and_aliased_limit_reads_correctly(tmp_path: Path):
    text = "jobs:\n  first:\n    timeout-minutes: &limit 20\n  second:\n    timeout-minutes: *limit\n"
    assert read_limits(write_workflow(tmp_path, text)) == {"w.yml: first": 20, "w.yml: second": 20}


def test_an_ordinary_workflow_with_json_or_script_in_block_text_is_accepted(tmp_path: Path):
    """The scanner this replaced refused these: `"blocks": [` looked like a value left open (T29, review round 2)."""
    text = """\
jobs:
  notify:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: slackapi/slack-github-action@v2
        with:
          payload: |
            {
              "text": "done",
              "blocks": [
                {"type": "section"}
              ]
            }
      - uses: actions/github-script@v7
        with:
          script: |
            const issue = {
              labels: [
                'bug',
              ],
            };
"""
    assert read_limits(write_workflow(tmp_path, text)) == {"w.yml: notify": 5}


def test_a_step_that_uses_an_action_is_not_a_reusable_workflow(tmp_path: Path):
    text = job("timeout-minutes: 5", "steps:", "  - uses: actions/checkout@v7")
    assert read_limits(write_workflow(tmp_path, text)) == {"w.yml: only": 5}


def test_both_yaml_spellings_are_read_and_nothing_else(tmp_path: Path):
    write_workflow(tmp_path, job("timeout-minutes: 5"), "one.yml")
    write_workflow(tmp_path, job("timeout-minutes: 7"), "two.yaml")
    write_workflow(tmp_path, "not: [a workflow", "notes.md")
    assert read_limits(tmp_path) == {"one.yml: only": 5, "two.yaml: only": 7}


# --- A bad workflow is rejected, and the failure names the file and the job ---


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('"20"', id="quoted"),
        pytest.param("0", id="zero"),
        pytest.param("-1", id="negative"),
        pytest.param("true", id="boolean true"),
        pytest.param("yes", id="boolean yes"),
        pytest.param("false", id="boolean false"),
        pytest.param("5.5", id="fraction"),
        pytest.param("5.0", id="whole number as a float"),
        pytest.param("", id="blank"),
        pytest.param("~", id="null"),
        pytest.param("${{ inputs.limit }}", id="expression"),
        pytest.param("[5]", id="a list"),
        pytest.param("{minutes: 5}", id="a mapping"),
    ],
)
def test_a_limit_that_is_not_a_positive_whole_number_is_rejected(tmp_path: Path, value: str):
    with pytest.raises(ValueError, match="w.yml: only: set timeout-minutes"):
        read_limits(write_workflow(tmp_path, job("runs-on: x", f"timeout-minutes: {value}")))


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param(["runs-on: x"], id="no limit"),
        pytest.param(["# timeout-minutes: 5", "runs-on: x"], id="in a comment"),
        pytest.param(["runs-on: x  # timeout-minutes: 5"], id="in a comment after another key"),
        pytest.param(["steps:", "  - run: x", "    timeout-minutes: 5"], id="on a step"),
        pytest.param(["strategy:", "  timeout-minutes: 5"], id="under another key"),
        pytest.param(["steps:", "  - run: |", "      timeout-minutes: 5"], id="inside block text"),
        pytest.param(["env: {", "timeout-minutes: 5", "}"], id="inside an open brace"),
        pytest.param(['name: "foo', "timeout-minutes: 5", 'bar"'], id="inside a multi-line quote"),
    ],
)
def test_a_job_with_no_limit_of_its_own_is_rejected(tmp_path: Path, properties: list[str]):
    with pytest.raises(ValueError, match="w.yml: only: set timeout-minutes"):
        read_limits(write_workflow(tmp_path, job(*properties)))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("timeout-minutes: 5\njobs:\n  only:\n    runs-on: x\n", id="at the workflow level"),
        pytest.param("jobs:\n  only:\n    runs-on: x\ndefaults:\n  timeout-minutes: 5\n", id="under another top key"),
        pytest.param("jobs:\n  first:\n    timeout-minutes: 5\n  only:\n    runs-on: x\n", id="on the job before it"),
        pytest.param("jobs:\n  only:\n", id="an empty job"),
        pytest.param("jobs:\n  only: |\n    timeout-minutes: 5\n", id="the job is block text"),
        pytest.param(
            "jobs:\n  only: {\n    env: {\n    timeout-minutes: 5\n    }, runs-on: x\n    }\n",
            id="inside nested braces",
        ),
        pytest.param("jobs:\n  only: 5\n", id="the job is a number"),
    ],
)
def test_a_limit_in_the_wrong_place_is_rejected(tmp_path: Path, text: str):
    with pytest.raises(ValueError, match="w.yml: only: set timeout-minutes"):
        read_limits(write_workflow(tmp_path, text))


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param(["uses: ./.github/workflows/shared.yml"], id="no limit"),
        pytest.param(["uses: ./.github/workflows/shared.yml", "timeout-minutes: 5"], id="a limit too"),
        pytest.param(['"uses": ./.github/workflows/shared.yml', "timeout-minutes: 5"], id="a quoted key"),
    ],
)
def test_a_job_that_calls_a_reusable_workflow_is_refused_whatever_it_sets(tmp_path: Path, properties: list[str]):
    with pytest.raises(ValueError, match="w.yml: only: calls a reusable workflow; a person must decide"):
        read_limits(write_workflow(tmp_path, job(*properties)))


# --- The gate can't pass by finding nothing ---


def test_an_empty_workflows_directory_fails(tmp_path: Path):
    with pytest.raises(ValueError, match="no workflow files"):
        read_limits(tmp_path)


def test_a_directory_with_no_workflow_files_fails(tmp_path: Path):
    with pytest.raises(ValueError, match="no workflow files"):
        read_limits(write_workflow(tmp_path, job("timeout-minutes: 5"), "notes.md"))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="an empty file"),
        pytest.param("# jobs:\n", id="only a comment"),
        pytest.param("name: CI\non: push\n", id="no jobs key"),
        pytest.param("jobs:\n", id="an empty jobs key"),
        pytest.param("jobs: {}\n", id="an empty mapping"),
        pytest.param("jobs: [only]\n", id="jobs as a list"),
        pytest.param("jobs: only\n", id="jobs as text"),
        pytest.param("- jobs\n", id="a list at the top"),
    ],
)
def test_a_file_with_no_jobs_fails_and_names_the_file(tmp_path: Path, text: str):
    with pytest.raises(ValueError, match="w.yml: no jobs"):
        read_limits(write_workflow(tmp_path, text))


def test_one_bad_file_fails_even_after_a_good_one(tmp_path: Path):
    write_workflow(tmp_path, job("timeout-minutes: 5"), "a.yml")
    write_workflow(tmp_path, job("runs-on: x"), "b.yaml")
    with pytest.raises(ValueError, match="b.yaml: only"):
        read_limits(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('jobs: {\n  name: "open\n', id="not valid YAML"),
        pytest.param("jobs:\n  a:\n    timeout-minutes: 5\n---\njobs:\n  b:\n    runs-on: x\n", id="two documents"),
    ],
)
def test_a_file_that_is_not_one_yaml_document_fails_and_names_the_file(tmp_path: Path, text: str):
    with pytest.raises(yaml.YAMLError, match="w.yml"):
        read_limits(write_workflow(tmp_path, text))
