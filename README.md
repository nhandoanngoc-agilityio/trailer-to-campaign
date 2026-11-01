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
uv run t2c eval                         # the Eval Set: how many Traps the Checker caught
```

`t2c watch` downloads the trailer from the Film's catalog link (or uses `--source`), samples frames and transcribes it locally, then has the Watcher pick 5–10 Key Moments. Each one is checked against what the Watcher was shown: frames it looked at, words heard in the transcript, cast from the Catalog Metadata. The Key Moments are written to `runs/<film>/<time>/key_moments.json`, next to the Run Log, and the Run stops if the Film Budget (`--budget`, or `FILM_BUDGET_USD`, default $2.00) is reached. A model call that doesn't answer within 10 minutes is cut off and charged what it might have cost; the agent then fails as if it gave up (the Watcher or the first writing ends the Run in an error, while a Checker review or a rewrite flags the posts it couldn't finish). `t2c dummy-run` checks the Film Budget meter without an API key.

`t2c write` has the Copywriter turn those Key Moments into six Social Posts: Facebook, Instagram and TikTok, each in English and Vietnamese. Every post must name the Film exactly, fit its platform, quote only Key Moment lines word for word (in English, even in Vietnamese posts), and cite a Key Moment or a catalog field for each fact it states. Then the Checker, the only agent that sees the Film's Spoiler List, reviews every post against that list, the catalog's cast and the brand voice. A rejected post goes back to the Copywriter, told only which of its own words to remove, and is checked again, at most twice. After that it is kept but **flagged** for a person, with the Checker's reasons. The posts are written to `social_posts.json` next to that Run's log, each one marked passed or flagged. `watch` and `write` are single stages, each a Run of its own, for trying one agent; `t2c run` below is the Run a marketer uses.

`t2c run` is the whole pipeline as one Run, under one Film Budget and one Run Log. The Watcher picks the Key Moments and you approve, edit (description and mood) or reject each one. The Copywriter and the Checker then write and check the Social Posts from the ones you kept, and you approve, edit or reject each post; an edit is re-checked and the result shown before you decide. Mark each edit a Light Edit or heavy (rewriting the hook is never light): the Run ends with its Approval Rate. Only your keystrokes approve: closing the terminal ends the Run in an error, never in an approval. The Run Log shows the spend, the retries and every decision, and `campaign.json` holds the result.

`t2c eval` runs the Eval Set (see `evals/README.md`): for each Film, the Checker reviews its Planted and Clean Drafts, then the agent team runs on its trailer with no Approval. It reports how many Traps the Checker caught, its false alarms and the cost per Film, and writes `report.md` and `summary.json` under `runs/_eval/<time>/`. The Films run side by side, so a run takes about as long as its slowest Film; `--parallel N` caps how many run at once (`--parallel 1` runs them in turn). Ctrl-C stops every Film in flight at its next step (footage work already running may finish first, with no output meanwhile, or stop with a footage error) and still writes the report for the Films that finished, with what the others were charged. Run it before merging any change to an agent's prompt, tool or model, and compare with the Baseline from `t2c eval --baseline`. `--only drafts` and `--key-moments-from` run only what a change touches (see `evals/README.md`).

`--watcher sdk` (on `run`, `watch`, `eval` and `slack`, or `T2C_WATCHER=sdk`) runs the Watcher on the Claude Agent SDK instead of its own loop, with the same tools, prompts and grounding (T11, compared in `docs/sdk-watcher.md`). v0 stays the default. The SDK Watcher is kept inside the Film Budget by a spending cap set before it starts and charges after each response (ADR 0009), so it needs a Film Budget of more than $1.12. The Eval Set report says which Watcher ran and gives its cost, grounding rejections and Key Moments per Film.

`t2c result --film <film> --old-way <min> --new-way <min>` writes the side-by-side page for a real launch (T12, see `docs/side-by-side.md`). Time Saved comes from two marketers' stopwatch times, one working the old way and one with Trailer-to-Campaign. The Approval Rate comes from the Run whose Campaign was used (the last to finish that gave one, or `--run <folder>`). Cost per Film is every launch Run of that Film added up, re-runs included; Eval Set and dummy Runs are left out. The page holds numbers only and is written next to the Run Logs, as `runs/<film>/result.md` and `result.json`.

### In Slack

Slack is the front door (ADR 0002). Create a Slack app (Socket Mode, bot scopes `app_mentions:read`, `chat:write`, `reactions:write`, the `app_mention` event, Interactivity on), install it, invite its bot to your channel, and export:

```
export SLACK_BOT_TOKEN=xoxb-...         # Install App: Bot User OAuth Token
export SLACK_APP_TOKEN=xapp-...         # Basic Information: App-Level Token with connections:write
export T2C_MARKETERS=U07ABC123          # the Marketers' Slack member ids, comma-separated
uv run t2c slack                        # runs until Ctrl-C
```

A **Marketer** starts a Run by mentioning the app (pick its bot from Slack's `@` list) with a Film, a trailer link, or both, e.g. `agent-327`. The app reacts 👀, refuses anyone not on `T2C_MARKETERS`, and refuses a Film with no `film.toml` (no Spoiler List, no Run). The Run lives in its **Run Thread**, under the mention: its progress, then each Key Moment and each Social Post with **Approve**, **Edit** and **Reject** buttons that only a Marketer's click decides. **Edit** opens a form with the draft's text (a Key Moment's description and mood, or a post); once saved, the thread shows how much the edit changed (the share of words) and, for a post, its re-check, and offers **Approve as Light Edit** or **Approve as heavy edit** (rewriting the hook is never light). One Run at a time; a mention inside an old thread starts a new Run in a new thread. Stopping `t2c slack` ends a Run that is waiting for Approval in an error, never in an approval.

Whether in the terminal or in Slack, the Run Log records every Approval decision, who made it (in Slack), and for each edit its Light/heavy mark and its **measured change**. The Run ends with the **Approval Rate** (drafts approved unedited or with a Light Edit), the average measured change of Light and heavy edits, and any Light Edit that changed over 30% of the words, worth a second look.
