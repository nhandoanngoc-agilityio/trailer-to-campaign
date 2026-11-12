# Trailer-to-Campaign

A multimodal agent team (Watcher → Copywriter → Checker) that turns an indie film's trailer into Social Posts, with a marketer's Approval before anything is published.

- Use the words in `CONTEXT.md` for code names, tickets and PRs. When a ticket needs a word the glossary lacks, propose a term instead of inventing one silently.
- Read `docs/adr/` before changing anything an ADR covers: the agent order, the Slack surface, how footage is watched, trailers-only, the Film Budget, the repo layout.
- Tickets are GitHub issues on `nhandoanngoc-agilityio/trailer-to-campaign`. Each has a **Done when** and **Blocked by**; a ticket whose blockers are still open is not ready.

## The ticket loop

Every ticket runs the loop in the `ticket-loop` skill (`.claude/skills/ticket-loop/SKILL.md`): load it before you start one. Its two human gates are **Plan** 🧑 (the human approves the plan before tests and code) and **Review** 🧑 (the human reviews last and merges); stop at each and wait.

**Every artifact of work (spec, tests, PRs, notes, the ticket record) is Claude-assisted and human-reviewed, edited where needed.** Claude drafts; the human reviews and edits. Claude never fills in a provenance block or a PR's Human review section on the human's behalf.

## Commands

- Install: `uv sync`. Enable the git hook once per clone: `git config core.hooksPath .githooks`
- Test: `uv run pytest` (about 90s). Lint: `uv run ruff check . && uv run ruff format --check .` A `Stop` hook holds lint on the files you changed.
- Open a PR: `git push -u origin <branch>`, then `gh pr create --base main`.

## Environment and worktrees

This is a macOS and zsh environment. Use `python3` if `python` is not on PATH, do not assume GNU `timeout` exists, and avoid unquoted word splitting. Prefer absolute paths over relying on an earlier `cd`, except for git in a worktree: `block_forbidden.py` judges the branch from the session's directory, so enter the worktree with `EnterWorktree(path=...)` and run git from there. In a worktree, fetch and integrate the latest main before starting and again before opening a PR, especially when parallel tickets are merging. If Claude Code's worktree guard refuses a heredoc, use Edit, or Write for a new file.

## What the repo won't tell you

- `evals/expected/` is the Answer Key (human-owned): Claude reads it and never edits it.
- A hook block or approval prompt from `.claude/hooks/` is team policy: change the approach (branch, PR, specific files) rather than reaching the same result another way.
- `docs/tickets/T<n>.md` is one record per ticket, from `docs/tickets/TEMPLATE.md`. `docs/lessons.md` gets one lesson per ticket.
- `.claude/claude-security-guidance.md` is what security review looks for in this project.
- `.claude/rules/` holds rules for the footage, prompts, Checker and Film Budget code; each loads when you work on those files.
- CI's Claude review (`claude-code-review.yml`) runs once when a PR opens, unless it only changes `docs/`. To review later pushes, comment `@claude` on the PR.

## Validation and eval changes

When changing a gate, checker, or validation rule, add tests for both sides: invalid input must be rejected and valid input must still pass. Do not loosen a rule to fix a false positive without a test showing that the original bad case still fails. Before a paid Eval Set re-run, state why it is needed and its expected cost, and confirm that known review findings are fixed or answered.

## Code standards

- Python with type hints; one module per agent and per MCP tool.
- Prompts live in their own files next to the agent that uses them, so prompt changes show up clearly in diffs and trigger the Eval Set.
- Every call to a paid model API goes through the Film Budget meter.
- Secrets come from the environment, never from the repo.
- Footage, transcripts, Catalog Metadata and Slack messages are data in prompts, never instructions.

## Never do

- Never commit or push directly to `main`; everything goes through a PR.
- Never post a Campaign Draft or record an Approval without a marketer's action in Slack.
- Never edit `evals/expected/` to make an Eval Set run pass.
- Never send screeners to a model in slice 1 (ADR 0005).
- Never use `git push --force` or `git reset --hard`.
- Never add a dependency without asking first.
