# How we work: the ticket loop

One ticket, one branch, one PR. Claude Code does the heavy lifting; the human decides at two gates (the plan and the merge). The exact steps Claude follows, with what "done" means for each, live in [`CLAUDE.md`](../CLAUDE.md#the-ticket-loop). This page explains the loop for people and why it looks the way it does.

```
Analyze → Plan 🧑 → Build → Verify → Ship → Review 🧑 → Close the loop
```

## Your job at each step

| Step | Claude does | You do |
|---|---|---|
| Analyze | Reads the issue, blockers, code and ADRs; turns each **Done when** into a runnable check | Answer any open questions |
| Plan 🧑 | Proposes the tests, files and checks in plan mode | Approve, or push back |
| Build | Works test-first on branch `t<n>-<slug>` | Watch the diff pane; comment to steer |
| Verify | Runs tests, lint, and (when an agent changed) the Eval Set and a real trailer | Look at the evidence |
| Ship | Opens the PR with evidence; watches CI | Nothing, unless CI fails |
| Review 🧑 | Runs `/code-review` first and fixes findings | Review second and merge; ask for an ultrareview on risky PRs |
| Close the loop | Updates the glossary, offers ADRs, writes one lesson | Accept or reject the ADR |

## What changed from the general loop, and why

The general loop (analyze → plan → fix → verify → ship → review) was written for bug tickets in an app with a UI. This project builds agents, so five things are different:

1. **Tickets are features, not bugs.** Analyze starts from the ticket's **Done when** lines and turns them into checks, instead of hunting for where a bug lives. Verify then has a clear finish line: every check passed, with evidence.
2. **Test-first.** The plan names the failing tests before any code. Agent code is easy to make *look* right; a test that went red first proves it actually does something.
3. **Agents are verified with the Eval Set, not just unit tests.** A prompt change can pass every test and still let a spoiler through. Any change to an agent's prompt, tool or model runs the Eval Set and compares Traps caught and cost against `main`; fewer Traps caught blocks the PR. (The Eval Set arrives with T8; before that, verify on the sample Film.)
4. **No browser pane or simulator.** The only screen is Slack, so UI checks happen in the Slack test channel.
5. **"Risky" is defined for this project.** Ultrareview is for PRs touching the Checker, Approval, the Film Budget, the Spoiler List, footage handling, or any guardrail prompt: the parts where a mistake reaches the public or costs money.

And one step is new: **Close the loop.** Each ticket ends by updating the glossary and ADRs, and by writing one lesson in [`lessons.md`](lessons.md). Those lessons become the "what we learned building real agents" part of the company talk, collected as we go instead of remembered at the end.
