---
name: review-checklist
description: Review a branch, PR or diff against this project's own rules and shipped bugs (the Checker, Approval, Spoiler List, Film Budget, footage, the ticket loop), reporting pass, fail or n/a per check with file:line. Use when reviewing a branch or PR, or self-reviewing before opening one.
---

# Review checklist

The checks `/code-review` and `/security-review` can't know: this project's rules, and the bugs it has already shipped. Each check names where it came from, so read that lesson or ADR when a check seems not to apply.

## 1. Find the diff

For a branch: `git diff origin/main...HEAD` and `git log origin/main..HEAD --oneline`. For a PR: `gh pr diff <n>`, `gh pr view <n>` and its commits.

Read every changed file in full. Many checks depend on the code around the change.

## 2. Apply every check

Mark each check **pass**, **fail** (file:line and one sentence why) or **n/a** (the diff doesn't touch that area). Done means every check below has a mark. A pass rests on code you read, and a run if the check needs one.

### The Checker and the retry loop
- [ ] Every Social Post reaches the Checker before Approval, including a Marketer's edit (T6, T10). See `src/trailer_to_campaign/agents/review.py`.
- [ ] The retry cap, `MAX_RETRIES`, is unchanged. A post still rejected after it stays **Flagged** and never reads as passed (T22).
- [ ] A guard that can't finish its job flags the post, and a failed review never counts as a pass.
- [ ] When the Checker lacks the data it needs, the fix gives it the data rather than a looser rule (T15).

### The Spoiler List
- [ ] Only the Checker can read the Spoiler List (ADR 0007). The Copywriter's servers stay catalog-only.
- [ ] What goes back to the Copywriter holds only its own words, in a fixed sentence (T6).
- [ ] Checker reasons go privately to Marketers, and stay out of the channel, because they can describe the spoiler (T22).
- [ ] A Film with no Spoiler List is refused. Only an explicit `spoilers = []` means none (T3).

### Approval
- [ ] Only a Marketer's action in Slack, or the person's keystrokes in `t2c run`, records an Approval. See `src/trailer_to_campaign/slack_approval.py`.
- [ ] Stopping or crashing ends the Run in an error, never in an Approval.
- [ ] The record says what happened: an approval over a flag, an edit's mark beside its Measured Change (T10, T22).

### Grounding and tracing
- [ ] What an agent claims is checked by code against what it was shown, not left to a prompt (T4, T5).
- [ ] Text that reaches a Marketer is checked at the character level (T16).
- [ ] A refused submission is kept in the Run Log as sent, so a failure can be read back (T18).

### The Film Budget
- [ ] Every call to a paid model API goes through the meter in `src/trailer_to_campaign/budget.py`. The Agent SDK is the one exception, capped and charged after the fact (ADR 0009).
- [ ] Any loop that calls a model has a stopping point, and stops at the Film Budget.
- [ ] A call that fails or times out is charged what it might have cost, and the timeout covers the whole call (T1, T17).
- [ ] A limit is sized from what the system may legitimately do, with real numbers behind it (T11, T17).

### Footage
- [ ] Trailers only: a screener never reaches a model (ADR 0005), and the footage is confirmed to be the trailer, not just a working link (T23).
- [ ] Links and file names reach ffmpeg and the downloader as data, through the checks in `src/trailer_to_campaign/footage/sources.py`.
- [ ] Footage, transcripts, Catalog Metadata and Slack messages are marked as data in every prompt that holds them.

### Prompts and the Eval Set
- [ ] A prompt lives in its own file next to the agent that uses it. An error message sent to a model counts as a prompt (T18).
- [ ] A change to a prompt, tool or model has the smallest Eval Set run that covers it, compared with the Baseline. Fewer Traps caught blocks the PR.
- [ ] `evals/expected/` is unchanged, whatever the reason.
- [ ] An eval or report checks that each expected result exists, as well as that nothing raised (T19). Only a whole, clean run counts as a Baseline (T20).

### Tests and evidence
- [ ] Each bug fix has a test that went **red** first, shown in the record or the PR. Commits each pass the tests, so they won't show it.
- [ ] A fake behaves as strictly as the real thing it replaces (T7).
- [ ] A test exercises the code where it really runs. A hook is tested on the machine's `python3`, and stays 3.9-compatible and fail-closed (T24).
- [ ] Each Done-when line has evidence that shows the claim itself. A flag names its reason, and a report states only what it saw (T12, T13).
- [ ] Real-footage or live-Slack evidence is there when the pipeline or Slack changed.

### The ticket loop
- [ ] The branch is `t<n>-<slug>`, and the PR uses `.github/pull_request_template.md` with `Closes #<n>`.
- [ ] **Risky** is ticked if the diff touches the Checker, Approval, the Film Budget, the Spoiler List, footage handling, or a prompt that enforces a guardrail.
- [ ] The PR's Human review section and the record's provenance block are left for the human.
- [ ] Code, tickets and PRs use the words in `CONTEXT.md`, and a new term was proposed rather than invented silently.
- [ ] `pyproject.toml` and `uv.lock` gain no dependency the human didn't agree to.
- [ ] The ticket record `docs/tickets/T<n>.md` and one line in `docs/lessons.md` are in the PR.

### Security
- [ ] Every rule in `.claude/claude-security-guidance.md` holds for the diff. Report each failure as its own row.

## Known non-issues

Report these only if the diff changes why they're fine:

- The "Not findings here" list in `.claude/claude-security-guidance.md`.
- The hook in `.claude/hooks/block_forbidden.py` is a tripwire for accidents, not a sandbox, and its known gaps are tracked in #70.
- Local models (faster-whisper) skip the meter.

## 3. Report

One table with a row per check, failures first:

| Check | Result | Where | Why |
|---|---|---|---|

Then a one-line verdict: **ready**, **ready after fixes** (list them), or **needs rework**. A review reports: fix only when asked.
