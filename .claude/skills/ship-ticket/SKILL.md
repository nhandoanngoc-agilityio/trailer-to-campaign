---
name: ship-ticket
description: Take a GitHub ticket through the ticket loop to a CI-green PR, stopping at the plan and merge gates
argument-hint: T<n> or issue number
disable-model-invocation: true
---

Ticket to ship: $ARGUMENTS

If that line names no ticket, ask which one. Otherwise run the ticket loop in `CLAUDE.md`, one step at a time. Each step there keeps its own requirements and **Done when**; below is only what this skill adds.

`CLAUDE.md` writes every number as `<n>`, but they differ: T26 is issue #73, and its PRs are #74 and #75. Keep them apart here:

- `<t>`: the ticket number, from the issue title `T<t>: ...`. It names the branch and `docs/tickets/T<t>.md`.
- `<issue>`: the GitHub issue, for `gh issue view` and `Closes #<issue>`. Given only a T number, find it with `gh issue list --state all --search "T<t>: in:title"`.
- `<pr>`: the number `gh pr create` returns, for every `gh pr` command.

## Steps

1. **Analyze.** Read the issue. If a **Blocked by** issue is still open, report it and stop. Otherwise set up the worktree, so the ticket record starts on the branch:
   - `git fetch origin main`, then `git worktree add -b t<t>-<slug> <absolute path to trailer-to-campaign-t<t>, beside the main checkout> origin/main`. If the branch or folder already exists, ask the human whether to resume it.
   - Move the session in with `EnterWorktree(path=...)`. `block_forbidden.py` judges the branch from the session's directory, so `git -C` and `cd` commits count as being on `main`. A session already in another worktree may be refused; if so, ask the human to start a new session in this one.
   - Run `uv sync`, and copy the main checkout's `.claude/settings.local.json` if it has one.

   Put open questions to the human and wait for the answers.
2. **Plan 🧑.** Use plan mode and stop until the human approves. Tests and code start after approval.
3. **Build.** Integrate main first (see **Integrate main**), since the plan may have waited a while.
4. **Verify.** Run every check the loop names for what changed. A paid run needs the key, which a worktree session can't load, and the Slack flow needs a person: give the human the exact command or steps, why it's needed and the expected cost, then wait for them to say it's done. `films/` and `runs/` stay in the main checkout, so a command that reads footage sets `T2C_FILMS_DIR` to that `films/` folder. A check nobody ran goes in the PR as not run.
5. **Ship.** Do the **Pre-PR checks** in `CLAUDE.md`, integrate main, and run `git push -u origin t<t>-<slug>`. Then open the PR with `gh pr create --base main --title "T<t>: <what changed>" --body-file <file>`, from `.github/pull_request_template.md`: `Closes #<issue>`, one evidence row per **Done when**, **Risky** ticked if it applies, and Human review left blank. Wait for CI.
6. **Review.** Run `review-checklist` and `/code-review` against `main`, then fix or answer those findings and the CI review's. After pushing a batch of fixes, wait for CI, comment `@claude` once, and wait up to 15 minutes for its reply on the PR. That reply isn't a PR check, so green CI doesn't mean it has arrived. If findings are still open after three batches, or no reply comes, stop and report it. If **Risky** is ticked, ask the human for an ultrareview.
7. **Close the loop.** In the PR, finish `docs/tickets/T<t>.md` with its provenance block left blank, add the lesson, update `CONTEXT.md`, offer an ADR if one meets the bar, and tick the template's boxes. Integrate main if it has moved, push, and wait for CI.
8. **Merge gate 🧑.** Stop once `gh pr view <pr> --json headRefOid,statusCheckRollup` shows `headRefOid` is your local `HEAD`, and that every check passed, including `test` and `answer-key`. An empty list means CI hasn't run, not that it passed. Report the PR link, that SHA, the evidence, anything not run, the caveats, and what's left for the human: an ultrareview if **Risky**, Human review, the provenance block and the merge.

   The human merges. Merge for them only if they ask after this report and Human review is filled in; a request made when the ticket started doesn't count. After the merge, confirm the issue closed.

## Integrate main

Run `git fetch origin main`, then `git merge origin/main`, then the full test suite and lint. Merging keeps a pushed branch fast-forward, so every push stays a plain push. If the merge conflicts in a way the plan didn't cover, report it and stop. If main's changes touch what Verify measured, say so, because that evidence may be out of date.

## Wait for CI

After the last push, run this with `run_in_background`. It ends by itself, so you get a notification even if CI hangs:

```sh
for i in {1..30}; do
  gh pr checks <pr>; code=$?
  [ "$code" -eq 0 ] && exit 0
  [ "$code" -eq 8 ] || [ "$i" -le 3 ] || exit "$code"
  sleep 30
done
echo "still not finished after 15 minutes"; exit 2
```

It checks every 30 seconds for up to 15 minutes. `gh` exits 8 while checks are pending, and 1 for a failed check or none reported, so the first three tries allow for checks that haven't registered yet.

- **Exit 0:** green. Step 8 still confirms the SHA, because this can read the previous commit's checks right after a push.
- **Exit 1, failed check:** read the failing log, fix the cause, push and wait again. After three rounds on the same failure, stop and report it.
- **Exit 1, no checks reported:** look for a merge conflict, because GitHub doesn't run `pull_request` workflows on one. Integrate main and push. If there's no conflict, report it.
- **Exit 2:** report the check as stuck. No workflow sets a timeout, and the slowest of the last 31 PR runs took under 4 minutes, so this usually means a hung test.
