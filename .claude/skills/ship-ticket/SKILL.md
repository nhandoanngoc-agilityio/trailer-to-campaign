---
name: ship-ticket
description: Take a GitHub ticket through the ticket loop to a CI-green PR, stopping at the plan and merge gates
argument-hint: <issue number>
disable-model-invocation: true
---

Ship ticket $ARGUMENTS by running the ticket loop in `CLAUDE.md`, one step at a time. Each step there keeps its own requirements and **Done when**; below is only what this skill adds. If $ARGUMENTS names no issue, ask which one.

1. **Analyze.** Read the issue. If a **Blocked by** issue is still open, report it and stop. Otherwise set up the worktree, so the ticket record starts on the branch:
   - `git fetch origin main`, then `git worktree add -b t<n>-<slug> <absolute path to trailer-to-campaign-t<n>, beside the main checkout> origin/main`.
   - Move the session in with `EnterWorktree(path=...)`. `block_forbidden.py` judges the branch from the session's directory, so `git -C` and `cd` commits count as being on `main`.
   - Run `uv sync`, and copy the main checkout's `.claude/settings.local.json` if it has one.

   Put open questions to the human and wait for the answers.
2. **Plan 🧑.** Use plan mode and stop until the human approves. Tests and code start after approval.
3. **Build.** Integrate main first (see **Integrate main**), since the plan may have waited a while.
4. **Verify.** Run every check the loop names for what changed. A paid run needs the key, which a worktree session can't load, and the Slack flow needs a person: give the human the exact command or steps, why it's needed and the expected cost, then wait for the output. A check nobody ran goes in the PR as not run.
5. **Ship.** Integrate main, then open the PR with `gh pr create --base main --body-file <file>`, from `.github/pull_request_template.md`: `Closes #<n>`, one evidence row per **Done when**, **Risky** ticked if it applies, and Human review left blank. Wait for CI.
6. **Review.** Run `review-checklist` and `/code-review` against `main`, then fix or answer those findings and the CI review's. After pushing fixes, comment `@claude` once per batch and wait for CI. If **Risky** is ticked, ask the human for an ultrareview.
7. **Close the loop.** In the PR, finish `docs/tickets/T<n>.md` with its provenance block left blank, add the lesson, update `CONTEXT.md`, offer an ADR if one meets the bar, and tick the template's boxes. Integrate main if it has moved, push, and wait for CI.
8. **Merge gate 🧑.** Stop once `gh pr view <n> --json headRefOid,statusCheckRollup` shows every check passed on the SHA of your local `HEAD`. Report the PR link, that SHA, the evidence, anything not run, the caveats, and what's left for the human: an ultrareview if **Risky**, Human review, the provenance block and the merge.

   The human merges. Merge for them only if they ask after this report and Human review is filled in; a request made when the ticket started doesn't count. After the merge, confirm the issue closed.

## Integrate main

Run `git fetch origin main`, then `git merge origin/main`, then the full test suite and lint. Merging keeps a pushed branch fast-forward, so every push stays a plain push. If the merge conflicts in a way the plan didn't cover, report it and stop.

## Wait for CI

Run `gh pr checks <n> --watch --fail-fast --interval 30` in the background, after the last push. If it says no checks are reported yet, try again a minute later, up to three times.

- **Time limit:** CI normally finishes in under 5 minutes, and no workflow sets a timeout. After 15 minutes, stop watching and report the check as stuck.
- **Red:** read the failing log, fix the cause, push and wait again. After three rounds on the same failure, stop and report it.
