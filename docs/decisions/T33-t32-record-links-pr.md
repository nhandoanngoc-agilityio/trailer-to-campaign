# T33 (#90), the T32 record links its pull request and shows the CI result

**Ticket:** T33 ([#90](https://github.com/nhandoanngoc-agilityio/trailer-to-campaign/issues/90)): the T32 record links its pull request and shows the CI result.

## Decision

The change edits one file, `docs/tickets/T32.md`, to fix findings 1 and 3 from the owner's review of PR #89. No code or tests change.

- The header swaps the template placeholder for `PR [#89](…/pull/89) · Done`. The issue link and the ` · ` separators stay as `TEMPLATE.md` lays them out.
- In the Evidence, "Re-run the suite in a full clone before merging." becomes the result of that re-run: PR #89's CI `test` job (linked) passed on head commit `d65ecc2` with `1882 passed, 1 deselected in 120.14s`, and its Python 3.9 hooks run gave `806 passed in 64.02s`. The local run and why it failed are kept.
- The Lint line keeps the local result and adds the same job's CI result: `All checks passed!` and `153 files already formatted`.

## Reasons

- The CI run is the full-clone re-run the record asked for, so it replaces the request rather than sitting next to it.
- The counts are quoted as well as linked, because GitHub Actions logs expire (90 days by default) and the Evidence must still read after the link dies.
- The commit is named so nobody reads a pre-merge run on the PR head as a run on `main`.
- CI's lint count (153) is the one to trust. The local count (139) is lower because files were missing from the checkout.
- `Done` is the status word the ticket asks for. The other merged records say `merged` or `merged <date>`, and the plan offers `merged 2026-11-27` as the alternative.

## Tests

`uv run pytest` exits 1 in this checkout: `391 failed, 1305 passed, 2 skipped, 1 deselected, 178 errors`. That is 569 known failures and 0 new ones, and all of them come from the checkout having no `.claude/` folder and no git repo, the same as before the change. No test reads `T32.md`.

## Not done

- Finding 2 from the #89 review (the comment in `test_version_without_package_metadata_says_unknown` claims more than the test checks). It needs a change to `tests/test_cli.py`, so the plan proposes a separate ticket.
- T32's Review table, its Provenance line (that one is the human's to fill in), `TEMPLATE.md`, other records' status words, and `docs/lessons.md`.
- No `docs/tickets/T33.md` or T33 lesson row: the plan treats this bookkeeping ticket as exempt. No Eval Set run, because no agent, prompt, Checker, Film Budget or footage code changed.
