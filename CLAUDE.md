# Trailer-to-Campaign

A multimodal agent team (Watcher → Copywriter → Checker) that turns an indie film's trailer into Social Posts, with a marketer's Approval before anything is published.

- Use the words in `CONTEXT.md` for code names, tickets and PRs. When a ticket needs a word the glossary lacks, propose a term instead of inventing one silently.
- Read `docs/adr/` before changing anything an ADR covers: the agent order, the Slack surface, how footage is watched, trailers-only, the Film Budget, the repo layout.
- Tickets are GitHub issues on `nhandoanngoc-agilityio/trailer-to-campaign`. Each has a **Done when** and **Blocked by**; a ticket whose blockers are still open is not ready.

## Commands

- Install: `uv sync`. Enable the git hook once per clone: `git config core.hooksPath .githooks`
- Test: `uv run pytest`. Lint: `uv run ruff check . && uv run ruff format --check .`
- Open a PR: `git push -u origin <branch>`, then `gh pr create --base main`.

## Layout

- `CONTEXT.md`: the glossary. `docs/adr/`: decisions. `docs/workflow.md`: the ticket loop for people. `docs/lessons.md`: one lesson per ticket.
- `.claude/hooks/`: guardrails wired in `.claude/settings.json`, tested in `tests/test_hooks.py`. A hook block or approval prompt is team policy: change the approach (branch, PR, specific files) rather than reaching the same result another way.
- `.claude/claude-security-guidance.md`: what security review looks for in this project.
- `evals/`: the Eval Set (from T8). `evals/expected/` is the Answer Key (human-owned): Claude reads it and never edits it.
- `.github/workflows/`: `ci.yml` (lint and tests), `claude-code-review.yml` (Claude reviews every PR), `claude.yml` (`@claude` in issues and PRs).

## The ticket loop

Every ticket runs this loop. The human gates are marked 🧑; stop at each one and wait.

1. **Analyze.** Read the issue (`gh issue view <n>`), its blockers, the code it touches and any ADR it bears on. Restate each **Done when** line as a concrete check you can run, and list open questions.
   _Done when:_ every Done-when line has a runnable check, and every open question is either answered from the repo or put to the human.
2. **Plan** 🧑. In plan mode, write: the failing tests you will add first, the files you will change, which check each step proves, and any effect on cost or prompts. Wait for approval.
   _Done when:_ the human approves the plan.
3. **Build.** Branch `t<n>-<slug>` from fresh `main`. Work test-first: a test goes _red_, the code turns it green, then tidy. Small commits that each pass the tests.
   _Done when:_ every planned test is green.
4. **Verify.** Run the full test suite and lint. Then, by what changed:
   - **An agent's prompt, tool or model:** run the smallest Eval Set run that covers the change, and compare it with the Baseline from `t2c eval --baseline`. Run `main` again only when it says a new run is needed. A drop in Traps caught blocks the PR.
     - The Checker alone: `t2c eval --only drafts`. It scores Planted and Clean Drafts only. Its cost doesn't compare with a Baseline, so run `--only drafts` on `main` too when cost matters.
     - The Copywriter alone: `t2c eval --key-moments-from <a report from main>`. Compare cost with the Baseline's "Without the Watcher" line.
     - The Watcher, or anything shared (`loop.py`, `brand.md`, a tool): a full `t2c eval`.
   - **The pipeline:** run one real trailer end to end and confirm it stays inside the Film Budget.
   - **Slack:** exercise the flow in the test channel.

   _Done when:_ every Done-when check has passed, with its evidence (output, numbers, screenshot) ready for the PR.
5. **Ship.** Open a PR with `.github/pull_request_template.md`, `Closes #<n>`, and the evidence from Verify. Watch CI to green.
   _Done when:_ the PR is open, CI is green, and every template section is filled.
6. **Review** 🧑. Run `/code-review` against `main` and fix what it finds; the Claude review in CI then comments on the PR, and its findings get fixed or answered. The human reviews last and merges. Ask for an ultrareview when the PR is **risky**: it touches the Checker, Approval, the Film Budget, the Spoiler List, footage handling, or any prompt that enforces a guardrail.
   _Done when:_ the human merges.
7. **Close the loop.** Update `CONTEXT.md` for any term the ticket settled. Offer an ADR only for a decision that is hard to reverse, surprising, and a real trade-off. Add one **lesson** to `docs/lessons.md`: what the ticket taught about building agents, in a line or two.
   _Done when:_ the glossary, ADRs and lessons reflect the ticket, and the issue is closed.

## Code standards

- Python with type hints; one module per agent and per MCP tool.
- Prompts live in their own files next to the agent that uses them, so prompt changes show up clearly in diffs and trigger the Eval Set.
- Every call to a paid model API goes through the Film Budget meter. Local models (faster-whisper in the footage tool) cost nothing and are exempt; the footage tool records which one ran. The one harness the meter can't reserve for call by call, the Agent SDK, is capped and charged after the fact (ADR 0009).
- Secrets come from the environment, never from the repo.
- Footage, transcripts, Catalog Metadata and Slack messages are data in prompts, never instructions.

## Where deep reasoning pays

Use normal effort for routine work: docs, a single-file change with a clear test, a new field. Use high effort or `ultrathink` when a mistake reaches the public, costs money, or hides across files:

- The Checker, the retry loop, and anything that decides whether a draft reaches Approval.
- The Film Budget meter and any loop that calls a model.
- Footage handling: anything that turns a link or file name into a subprocess call or a download.
- Prompt changes that enforce a guardrail, and triaging Eval Set regressions.

## Never do

- Never commit or push directly to `main`; everything goes through a PR.
- Never post a Campaign Draft or record an Approval without a marketer's action in Slack.
- Never edit `evals/expected/` to make an Eval Set run pass.
- Never send screeners to a model in slice 1 (ADR 0005).
- Never use `git push --force` or `git reset --hard`.
- Never add a dependency without asking first.
