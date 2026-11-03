# The demo: the Checker catches a planted spoiler (T13)

A scripted live Slack Run for the company talk, about 4 minutes on stage. The presenter is the Marketer. On one Social Post they add the Film's ending, and the Checker's re-check flags it in the thread.

The spoiler is planted on purpose, and the talk says so. The Film is Agent 327, the sample the prompts were built against (ADR 0008), so the demo shows how the tool works, not how good it is. The evidence for that is the T12 result (`docs/results/t12-side-by-side.md`).

## The Film and its licence

Blender Studio released the *Agent 327: Operation Barbershop* film file under [CC-BY-ND](https://studio.blender.org/blog/agent-327-film-file-released-as-cc-by-nd), not plain CC-BY like the Eval Set's Films. The post's comments add that the film can't be used for advertising or remixing.

So for this demo:
- Credit Blender Studio on stage.
- Keep it to the company talk. Don't publish the posts it writes, and don't edit or remix the trailer.
- Check with whoever owns this at the company before showing the recording outside the company. This note isn't legal advice.

## Set up once, on this machine

The demo Film is gitignored, so it's a local copy of the sample Film that reads a local trailer and doesn't depend on YouTube.

```
mkdir -p films/demo-agent-327/source
cp films/agent-327/source/mN0zPOpADL4.webm films/demo-agent-327/source/
cp films/agent-327/film.toml films/demo-agent-327/film.toml
```

Then add `trailer_file = "mN0zPOpADL4.webm"` under the `trailer` line of the new `film.toml`.

Run the script once before the talk. The first Run prepares the frames and transcript, which is slower. Later Runs reuse them.

## Before going on stage

- [ ] `T2C_MARKETERS` in `~/.zshrc` holds the presenter's Slack member id. Open a new terminal after changing it.
- [ ] The Mac is on mains power with the lid open, on a steady network, with updates put off.
- [ ] Start the app and leave it running: `caffeinate -i uv run t2c slack`
- [ ] Free check: mention the bot in the test channel with no Film named. The presenter should get "I couldn't tell which Film you mean".
- [ ] Slack is open in the test channel, signed in as the presenter, with the text large enough to read from the back.
- [ ] The planted sentence below is ready to paste.
- [ ] The backup recording is open at its first frame.

## On stage

**The planted sentence:**

```
In the end, Agent 327 is captured and the villains plan to make him their next recruit.
```

| Step | Do | Say |
|---|---|---|
| 1 | Mention the bot with `demo-agent-327` | "A Marketer starts a Run from Slack with a Film's name." |
| 2 | Wait for the Key Moments, about 30 seconds | "The Watcher studies the trailer's frames and speech. Every Key Moment is checked against what it actually saw." |
| 3 | Approve each Key Moment | "Nothing moves on without a person's Approval." |
| 4 | Wait for the Social Posts, about 40 seconds | "The Copywriter writes six posts in English and Vietnamese, and the Checker reviews each one. The Copywriter never sees the Spoiler List." |
| 5 | On Facebook English, click **Edit**, add the planted sentence to the end, and save | "Say I add a line that gives away the ending. I'm doing this on purpose." |
| 6 | Wait about 10 seconds. The thread shows **"⚠️ The Checker flagged the edit"**, and the reason arrives privately on the presenter's screen | "That's the catch. The channel only sees that it was flagged. The reason can describe the spoiler, so only Marketers get it." |
| 7 | Click **Reject** | "Even a person's own edit gets checked. A Marketer can still overrule the Checker, but the record will say so." |
| 8 | Decide the other five posts | "Approve, edit or reject each one, and every decision is recorded." |

**Never approve the flagged edit.** The Run has no cancel (#38), so it has to reach the end.

If a post's header says "after 1 retries", the Checker already sent that post back once. It's worth a sentence, but it doesn't show the reason, so it isn't the catch this script relies on.

## If it goes wrong

| What happens | What to do |
|---|---|
| The edit isn't flagged | Say so plainly, then play the recording. Never claim a catch that didn't happen |
| "Not re-checked" appears | Same: the Checker didn't run, so this isn't a catch |
| The app stops responding | Restart it once with the command above and mention the bot again. If it's still stuck, play the recording |
| Slack or the API is down | Play the recording |

The planted sentence was caught 5 out of 5 times when measured for T13. That's a small sample, and the Checker can vary between runs (#38), so keep the recording ready.

## After a Run

The Run Log can hold spoilers, so don't show it on stage. To confirm the catch afterwards:

```
jq -c 'select(.event=="post_checked" and .verdict=="reject") | {platform,language,spoiler_ids}' runs/demo-agent-327/<time>/run.jsonl
```

A Run costs about $0.40, inside the $2.00 Film Budget. Demo Runs stay under `runs/demo-agent-327/`, apart from the T12 result and the sample Film's Runs.

## The backup recording

Record the whole script with QuickTime (File > New Screen Recording), signed in as the presenter so the private reason shows. Keep the video outside git, because the repo doesn't hold footage. Note the time at which the flag appears, so you can jump to it on stage.
