# How we work: the ticket loop

One ticket, one branch, one PR. Claude Code does the heavy lifting; the human decides at two gates (the plan and the merge). The exact steps Claude follows, with what "done" means for each, live in [`CLAUDE.md`](../CLAUDE.md#the-ticket-loop). This page explains the loop for people and why it looks the way it does.

```
Analyze → Plan 🧑 → Build → Verify → Ship → Review 🧑 → Close the loop
```

## Your job at each step

| Step | Claude does | You do |
|---|---|---|
| Analyze | Reads the issue, blockers, code and ADRs; turns each **Done when** into a runnable check; starts the ticket record | Answer any open questions |
| Plan 🧑 | Proposes the tests, files and checks in plan mode | Approve, or push back |
| Build | Works test-first on branch `t<n>-<slug>` | Watch the diff pane; comment to steer |
| Verify | Runs tests, lint, and (when an agent changed) the Eval Set and a real trailer | Look at the evidence |
| Ship | Opens the PR with evidence; watches CI | Nothing, unless CI fails |
| Review 🧑 | Runs `/code-review` first and fixes findings; Claude's CI review then comments on the PR | Review last and merge; ask for an ultrareview on risky PRs |
| Close the loop | Finishes the ticket record, updates the glossary, offers ADRs, writes one lesson | Review the record and edit where needed; fill in its provenance and the PR's Human review; accept or reject the ADR |

## Claude drafts, people review

Every artifact of work (the spec, tests, PRs, notes, and the ticket record in `docs/tickets/T<n>.md`) is Claude-assisted and human-reviewed, edited where needed. Claude does the drafting; a person reads it and changes what's wrong. That review is recorded where only the person fills it in: the provenance line at the end of each ticket record, and the **Human review** section of each PR. A PR isn't ready to merge until that section is filled in.

## Guardrails: rules the tools enforce

Instructions in `CLAUDE.md` guide Claude; these make the important ones deterministic, so they hold even when an instruction is missed. They follow the AI-native SDLC setup from the training repos (`claude-training`, `release-planner`), adapted to an agent product.

| Rule | Enforced by |
|---|---|
| Everything goes through a PR; no force-push, no `reset --hard`, no `rm -rf` | `block_forbidden.py` hook (Claude), `.githooks/pre-commit` (any terminal) |
| Secrets stay out of the repo | `block_forbidden.py` blocks staging `.env`; settings deny Claude reading it; `.gitignore` |
| New dependencies are a human decision | `block_forbidden.py` asks before `uv add` / `pip install` |
| The Answer Key can't be gamed | Settings deny Claude edits to `evals/expected/` |
| A prompt change runs the Eval Set | `prompt_changed.py` reminds Claude after any edit under `prompts/` |
| Code is formatted the same way | `format_on_edit.py` runs ruff on every Python edit; CI checks it |
| Security issues specific to this product get caught | Security plugins + `.claude/claude-security-guidance.md` |
| Every PR is tested, and reviewed once when it opens unless it only changes `docs/` (`@claude` re-reviews) | `ci.yml` (lint, tests, hook tests), `claude-code-review.yml` |
| You know when Claude is waiting | `notify_on_finish.py` desktop notification |

The hooks have their own tests (`tests/test_hooks.py`), so a guardrail can't silently stop working. They run on the machine's `python3`, not the project's, and macOS ships 3.9, so the tests and CI run them on that too (T24).

**One-time setup:** run `git config core.hooksPath .githooks` in each clone, and add the `CLAUDE_CODE_OAUTH_TOKEN` secret to the repo so the Claude review and `@claude` workflows can run.

## What changed from the general loop, and why

The general loop (analyze → plan → fix → verify → ship → review) was written for bug tickets in an app with a UI. This project builds agents, so five things are different:

1. **Tickets are features, not bugs.** Analyze starts from the ticket's **Done when** lines and turns them into checks, instead of hunting for where a bug lives. Verify then has a clear finish line: every check passed, with evidence.
2. **Test-first.** The plan names the failing tests before any code. Agent code is easy to make *look* right; a test that went red first proves it actually does something.
3. **Agents are verified with the Eval Set, not just unit tests.** A prompt change can pass every test and still let a spoiler through. Any change to an agent's prompt, tool or model runs the Eval Set and compares Traps caught and cost against `main`; fewer Traps caught blocks the PR. (The Eval Set arrives with T8; before that, verify on the sample Film.)
4. **No browser pane or simulator.** The only screen is Slack, so UI checks happen in the Slack test channel.
5. **"Risky" is defined for this project.** Ultrareview is for PRs touching the Checker, Approval, the Film Budget, the Spoiler List, footage handling, or any guardrail prompt: the parts where a mistake reaches the public or costs money.

And one step is new: **Close the loop.** Each ticket ends by updating the glossary and ADRs, and by writing one lesson in [`lessons.md`](lessons.md). Those lessons become the "what we learned building real agents" part of the company talk, collected as we go instead of remembered at the end.
