# Slice 1 tickets

Three weeks to a slice the marketing team uses on one real Film. Each ticket is a thin, testable piece; "Blocked by" lists the tickets that must be done first. Terms follow [`CONTEXT.md`](../../CONTEXT.md). Each ticket Tn is GitHub issue #n; the issues are the live version, with blocking links and `week-N` labels.

**Week 1:** T1–T7, a working pipeline from the command line.
**Week 2:** T8–T11, evals, Slack and Approval, Agent SDK.
**Week 3:** T12–T13, the real Film, the numbers and the demo.

Critical path: T1 → T2/T3 → T4 → T5 → T6 → T7 → T9 → T10 → T12 → T13. T8 and T11 run alongside T9–T10 in week 2.

---

## T1 — Project skeleton and Film Budget meter

Set up the Python project (dependency management, config for the Anthropic API key, a `films/` folder layout, logging). Add a cost meter that records model spend per run and stops the run when the Film Budget is reached.

- **Done when:** a dummy run with a tiny budget stops with a clear "Film Budget reached" message, and spend is written to the run log.
- **Blocked by:** none

## T2 — Footage tool: frames and transcript

An MCP server that takes a trailer link or file and returns sampled frames (fixed interval plus scene changes, via ffmpeg) and a timestamped speech transcript. Official trailers only.

- **Done when:** for one real trailer it returns frames with timestamps and a transcript, and rejects anything marked as a screener.
- **Blocked by:** T1

## T3 — Film tool: Catalog Metadata and Spoiler List

An MCP server that returns a Film's Catalog Metadata and its Spoiler List. Start with one file per Film (no database yet).

- **Done when:** the agents can look up title, synopsis, genre, cast, language and the Spoiler List for a sample Film.
- **Blocked by:** T1

## T4 — Watcher v0, hand-written loop

Write the Watcher's agent loop by hand on the Claude API (no SDK): it reads frames, transcript and Catalog Metadata through the tools and produces Key Moments (timestamp, mood, quotable line).

- **Done when:** for one trailer it produces 5–10 Key Moments whose timestamps match the footage and whose cast names match Catalog Metadata.
- **Blocked by:** T2, T3

## T5 — Copywriter

Turns Key Moments into Social Posts in English and Vietnamese, one per platform, using only facts from the Key Moments and Catalog Metadata.

- **Done when:** for one Film it produces posts in both languages, and every factual claim traces back to a Key Moment or Catalog Metadata.
- **Blocked by:** T4

## T6 — Checker and the retry loop

Reviews each Social Post against the Spoiler List and brand rules. A rejected post goes back to the Copywriter with reasons, at most 2 retries; after that it is handed to a human with the problem flagged.

- **Done when:** a post that reveals a Spoiler List item is caught, retried, and either fixed or flagged after 2 tries. No post skips the Checker.
- **Blocked by:** T5, T3

## T7 — End-to-end pipeline from the command line

One command runs Watcher → Copywriter → Checker for a Film, pauses for the two Approval points (Key Moments, then Social Posts) in the terminal, and respects the Film Budget.

- **Done when:** a full run on one real trailer completes within budget, and the run log shows spend, retries and Approval decisions.
- **Blocked by:** T1, T4, T5, T6

## T8 — Eval Set and eval command

Collect 3–5 past Films with the posts the team actually published. Plant Traps (Spoiler List items, wrong cast names). One command runs the pipeline on the whole Eval Set and reports Traps caught, Approval-style quality notes and cost.

- **Done when:** the eval command runs unattended and reports how many Traps the Checker caught, out of how many.
- **Blocked by:** T7

## T9 — Slack front door

A Slack app: a marketer drops a trailer link in a channel, and the run posts progress and Campaign Drafts in a thread.

- **Done when:** dropping a trailer link in the test channel produces Key Moments and Social Posts in the thread.
- **Blocked by:** T7

## T10 — Approval in Slack

Both Approval points in Slack: approve, edit or reject; for an approved draft, the marketer marks it Light Edit or heavy. Record each decision, the edited text, and the measured share of text changed.

- **Done when:** Approval Rate for a run can be read from the records, alongside the measured change as a sanity check.
- **Blocked by:** T9

## T11 — Watcher on the Claude Agent SDK

Rebuild the Watcher on the Claude Agent SDK, keeping the same tools. Compare it with the hand-written v0 on the Eval Set.

- **Done when:** the SDK Watcher scores at least as well as v0 on the Eval Set, and the comparison is written down for the talk.
- **Blocked by:** T4, T8

## T12 — Real Film, side by side

Pick one upcoming Film. One marketer prepares its posts the old way while another uses Trailer-to-Campaign. Record both times, Approval Rate and cost.

- **Done when:** there is a one-page result with Time Saved, Approval Rate and cost per Film for that real launch.
- **Blocked by:** T8, T10

## T13 — Demo kit for the company talk

A scripted live Slack run on a trailer with a planted spoiler the Checker catches, a recorded backup of the same run, and the side-by-side slide from T12.

- **Done when:** the live run and the backup recording both show the Checker catch, and the slide is ready.
- **Blocked by:** T12

---

## Not in slice 1

Clip cut-list, Strategist (ported from `release-planner`), Orchestrator, screeners, localization agent. See ADRs 0004–0006.
