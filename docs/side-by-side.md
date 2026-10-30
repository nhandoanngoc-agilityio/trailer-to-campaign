# Side by side: one real Film, two ways (T12)

One upcoming Film is prepared twice, so Time Saved, Approval Rate and cost per Film are measured on a real launch, not assumed. Marketer A prepares the posts the old way. Marketer B uses Trailer-to-Campaign. Both follow this page, so both time the same work.

## Before you start

- **The Film:**
  - It is upcoming and has a public, official trailer link. Trailers only, never a screener (ADR 0005).
  - The Film's team writes `films/<film>/film.toml`: catalog facts, the trailer link and the Spoiler List (see `films/README.md`).
  - That folder is gitignored, because the Spoiler List is unreleased plot.
- **Who does which:**
  - Marketer A and Marketer B work at the same time, or B goes first.
  - Neither sees the other's posts until both are done. Whoever sees the other's work first is no longer timing the same task.
- **The deliverable is the same on both sides:** six Social Posts, for Facebook, Instagram and TikTok, each in English and Vietnamese, ready to publish.
- **Each side has a stopwatch:** a phone timer is fine.

## What to time

- **Start** the stopwatch when you have the trailer link and the catalog facts in front of you.
- **Stop** it when all six posts are ready to publish:
  - **A, the old way:** written, checked for spoilers by a person against the Spoiler List, and signed off as you normally would.
  - **B, Trailer-to-Campaign:** every post approved in Slack (or `t2c run`), plus any fixing you did afterwards outside the tool.
- **Time active work only.** Pause for breaks, meetings and lunch. On B's side, pause while the agents run. Waiting for them is not your work, but it is in the Run Log, which the page shows beside the stopwatch.
- **If a Run fails and you start the Film again,** keep B's stopwatch going for any work you redo. Every Run's cost counts on the page anyway.

Write your minutes down as soon as you stop: A's and B's, to the minute.

## Then

```
uv run t2c result --film <film> --old-way <A's minutes> --new-way <B's minutes>
```

This writes `runs/<film>/result.md`, plus `result.json` with the same numbers:
- **Time Saved:** A's minutes minus B's. If B took longer, the page says so.
- **Approval Rate:** from the Run whose Campaign was used. By default that's the last Run to finish that gave a Campaign (completed, with at least one Social Post approved). If that isn't the one you used, name it: `--run <its folder under runs/<film>/>`.
- **Cost per Film:** every Run of the Film added up, re-runs and failed Runs included, since the Film Budget caps each Run, not the Film. Eval Set and `dummy-run` Runs are left out, because they aren't launch work.
- Each Run's Run Log time, beside the stopwatch, as a check.

The pages hold the Film's title and numbers only, never a post or a plot point. They stay gitignored next to the Run Logs. Once the team agrees, a copy of both goes to `docs/results/` for the talk (T13).
