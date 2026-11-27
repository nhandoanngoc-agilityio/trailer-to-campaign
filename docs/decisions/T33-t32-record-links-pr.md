# T33 (#90): the T32 record links its pull request and shows the CI result

**Ticket:** [#90](https://github.com/nhandoanngoc-agilityio/trailer-to-campaign/issues/90), T33: The T32 record links its pull request and shows the CI result.

## Decision

A docs-only bookkeeping fix to `docs/tickets/T32.md`. No code or tests change.

- The header replaces the template placeholder `PR [#<pr>](…/pull/<pr>)` with a link to PR #89, and the status `in Build` becomes `Done`.
- Evidence gains the CI result of PR #89: the `test` job passed in a full clone (run 45934832142, job 140295513186). Lint passed, `uv run pytest -q` gave `1882 passed, 1 deselected`, and the hook tests on Python 3.9 gave `806 passed`. The `answer-key` and `claude-review` checks also passed.
- The local-run paragraph keeps its result and explanation. Its closing "Re-run the suite in a full clone before merging." becomes "The full-clone run is CI's, below."

## Reasons

- The ticket asks for exactly `Done`, and the ticket outranks the review's `in Review` and the `merged <date>` habit of other records.
- The pass counts come from the CI job log. If the log could not have been read, the record would have given the link and said so rather than guess a number.
- The local run is kept as honest history. Only the instruction that had already been carried out is replaced.
- The Done-when allows changes to `T32.md` only. T33 is not mentioned there, because git history and PR #90 record the correction.

## Tests

No tests were added or changed, since no test reads ticket headers or Evidence (`test_claude_setup.py` skips `docs/tickets/`). `uv run pytest -rfE -q` gave `391 failed, 1305 passed, 2 skipped, 1 deselected, 178 errors`, the same as the baseline before the change. Every failure and error comes from the build checkout, which has no `.claude/` folder and is not a git repository.

## Not done

- PR #89 review findings 2 (a test comment that claims more than it checks) and 4 (no visible Plan approval).
- T32's Review table and Provenance line, which are left for the human.
- A `docs/tickets/T33.md` record and a T33 lesson in `docs/lessons.md`.
- Any change to `TEMPLATE.md`, other records, code or CI.
