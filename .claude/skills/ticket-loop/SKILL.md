---
name: ticket-loop
description: The loop every ticket (T<n>, a GitHub issue with Done when and Blocked by) runs, from Analyze to Close the loop, with its two human gates and the Verify checks per kind of change. Use when starting, planning, building, verifying, shipping, reviewing or closing a ticket, or before opening a PR.
---

# The ticket loop

Every ticket runs this loop. The human gates are marked 🧑; stop at each one and wait. To drive a whole ticket to a PR, the human can start `/ship-ticket`, which runs this loop with its own extra steps.

**Every artifact of work (spec, tests, PRs, notes, the ticket record) is Claude-assisted and human-reviewed, edited where needed.** Claude drafts; the human reviews and edits. Claude never fills in a provenance block or a PR's Human review section on the human's behalf.

1. **Analyze.** Read the issue (`gh issue view <n>`), its blockers, the code it touches and any ADR it bears on. Restate each **Done when** as a concrete check you can run, and list open questions.
   If its blockers are closed, branch `t<n>-<slug>` from fresh `main` and start `docs/tickets/T<n>.md` there, from the template with the Spec section.
   _Done when:_ every Done-when line has a runnable check, and every open question is either answered from the repo or put to the human.
2. **Plan** 🧑. In plan mode, write: the failing tests you will add first, the files you will change, which check each step proves, and any effect on cost or prompts. If the work is one of the areas under **Where deep reasoning pays**, say so in the plan, so the human can raise the effort or ask for `ultrathink`. Wait for approval.
   _Done when:_ the human approves the plan.
3. **Build.** If `main` has moved, merge it in. Work test-first: a test goes _red_, the code turns it green, then tidy. Small commits that each pass the tests.
   _Done when:_ every planned test is green.
4. **Verify.** Run the full test suite and lint. Then, by what changed:
   - **An agent's prompt, tool or model:** run the smallest Eval Set run that covers the change, and compare it with the Baseline from `t2c eval --baseline`. Run `main` again only when it says a new run is needed. A drop in Traps caught blocks the PR.
     - The Checker alone: `t2c eval --only drafts`. It scores Planted and Clean Drafts only. Its cost doesn't compare with a Baseline, so run `--only drafts` on `main` too when cost matters.
     - The Copywriter alone: `t2c eval --key-moments-from <a report from main>`. Compare cost with the Baseline's "Without the Watcher" line.
     - The Watcher, or anything shared (`loop.py`, `brand.md`, a tool): a full `t2c eval`.
   - **The pipeline:** run one real trailer end to end and confirm it stays inside the Film Budget.
   - **Slack:** exercise the flow in the test channel.

   _Done when:_ every Done-when check has passed, with its evidence (output, numbers, screenshot) ready for the PR.
5. **Ship.** Do the **Pre-PR checks** below. Open a PR with `.github/pull_request_template.md`, `Closes #<n>`, and the evidence from Verify. Watch CI to green.
   _Done when:_ the PR is open, CI is green, and every template section is filled.
6. **Review** 🧑. Run the `review-checklist` skill and `/code-review` against `main`, and fix what they find; the `reviewer` subagent can also check the diff against the ticket's Spec. The Claude review in CI comments on the PR when it opens, and its findings get fixed or answered. It doesn't run again on later pushes: comment `@claude` on the PR to review them. The human reviews last and merges; under the `ship-ticket` skill, that review happens once, after Close the loop, so the record is in the PR. Ask for an ultrareview when the PR is **risky**: it touches the Checker, Approval, the Film Budget, the Spoiler List, footage handling, or any prompt that enforces a guardrail.
   _Done when:_ the human merges.
7. **Close the loop.** Finish `docs/tickets/T<n>.md` (plan, tests, review outcomes, evidence, close) in the ticket's PR, leaving its provenance block for the human. Update `CONTEXT.md` for any term the ticket settled. Offer an ADR only for a decision that is hard to reverse, surprising, and a real trade-off. Add one **lesson** to `docs/lessons.md`: what the ticket taught about building agents, in a line or two.
   _Done when:_ the ticket record, glossary, ADRs and lessons reflect the ticket, the human has reviewed the record, and the issue is closed.

## Pre-PR checks

Before opening a PR, review your own diff for wrong-number errors, budget or limit bugs, unhandled edge cases, and tests that can hang. Run the full test suite and lint, and report any issues you could not fix. (The `Stop` hook already holds lint on the files you changed; the full suite and the whole-repo lint are still yours to run.)

## Where deep reasoning pays

Normal effort fits routine work: docs, a single-file change with a clear test, a new field. These are where a mistake reaches the public, costs money, or hides across files, so check edge cases harder and tell the human when higher effort or `ultrathink` is worth it:

- The Checker, the retry loop, and anything that decides whether a draft reaches Approval.
- The Film Budget meter and any loop that calls a model.
- Footage handling: anything that turns a link or file name into a subprocess call or a download.
- Prompt changes that enforce a guardrail, and triaging Eval Set regressions.

The rules in `.claude/rules/` say the same for each area's files, and load when you work there.
