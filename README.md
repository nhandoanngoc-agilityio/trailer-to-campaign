# Trailer-to-Campaign

A multimodal agent team that turns an indie film's trailer into a launch campaign. The marketing team drops a trailer into Slack; the **Watcher** studies it, the **Copywriter** writes Social Posts in English and Vietnamese, the **Checker** reviews them against the Film's Spoiler List and the brand, and a marketer gives Approval before anything is published.

The point is not a demo. Most agent projects never leave the pilot stage; this one is built to be used every launch, and to prove its value with two numbers: **Time Saved** and **Approval Rate**.

## Where things are

- [`CONTEXT.md`](CONTEXT.md): the project's language. Use these words in code, tickets and talks.
- [`docs/adr/`](docs/adr/): the decisions that shaped the project, and the options we rejected.
- [`docs/tickets/slice-1.md`](docs/tickets/slice-1.md): the 3-week plan for the first usable slice.
- [`docs/workflow.md`](docs/workflow.md): how each ticket goes from issue to merged PR, and who decides what.
- [`docs/lessons.md`](docs/lessons.md): what each ticket taught us about building agents.

## Slice 1 at a glance

| | |
|---|---|
| Agents | Watcher → Copywriter → Checker, fixed order (ADR 0004) |
| Input | Official trailers only (ADR 0005) |
| How it "watches" | Sampled frames plus a speech transcript (ADR 0003) |
| Where people use it | Slack, with tools as MCP servers underneath (ADR 0002) |
| Approval | Key Moments first, then each Social Post |
| Guardrails | Per-Film Spoiler List, Checker retries capped at 2, hard Film Budget |
| Proof | Eval Set with Traps; side-by-side Time Saved on one real Film |
| Stack | Python, Claude Agent SDK (Watcher loop hand-written first), ffmpeg, speech-to-text |

## Run it

`uv sync`, then:

```
export ANTHROPIC_API_KEY=...            # only commands that call a model need it
uv run t2c run --film agent-327         # one whole Run, with your Approval at each step
uv run t2c watch --film agent-327       # or one stage at a time: the Watcher on the trailer
uv run t2c write --film agent-327 --key-moments runs/agent-327/<time>/key_moments.json
```

`t2c watch` downloads the trailer from the Film's catalog link (or uses `--source`), samples frames and transcribes it locally, then has the Watcher pick 5–10 Key Moments. Each one is checked against what the Watcher was shown: frames it looked at, words heard in the transcript, cast from the Catalog Metadata. The Key Moments are written to `runs/<film>/<time>/key_moments.json`, next to the Run Log, and the Run stops if the Film Budget (`--budget`, or `FILM_BUDGET_USD`, default $2.00) is reached. `t2c dummy-run` checks the Film Budget meter without an API key.

`t2c write` has the Copywriter turn those Key Moments into six Social Posts: Facebook, Instagram and TikTok, each in English and Vietnamese. Every post must name the Film exactly, fit its platform, quote only Key Moment lines word for word (in English, even in Vietnamese posts), and cite a Key Moment or a catalog field for each fact it states. Then the Checker, the only agent that sees the Film's Spoiler List, reviews every post against that list and the brand voice. A rejected post goes back to the Copywriter, told only which of its own words to remove, and is checked again, at most twice. After that it is kept but **flagged** for a person, with the Checker's reasons. The posts are written to `social_posts.json` next to that Run's log, each one marked passed or flagged. Until T7 joins them into one Run, `watch` and `write` each start a Run of their own.

`t2c run` is the whole pipeline as one Run, under one Film Budget and one Run Log. The Watcher picks the Key Moments and you approve, edit (description and mood) or reject each one. The Copywriter and the Checker then write and check the Social Posts from the ones you kept, and you approve, edit or reject each post; an edit is re-checked and the result shown before you decide. Mark each edit a Light Edit or heavy (rewriting the hook is never light): the Run ends with its Approval Rate. Only your keystrokes approve: closing the terminal ends the Run in an error, never in an approval. The Run Log shows the spend, the retries and every decision, and `campaign.json` holds the result.
