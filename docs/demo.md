# The demo: the Checker catches a planted spoiler (T13)

A scripted live Slack Run for the company talk, about 6 minutes on stage. The presenter is the Marketer. On one Social Post they add the Film's ending, and the Checker's re-check flags it in the thread.

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
- [ ] The demo Film is ready. This costs nothing, and should print the trailer's file name:
  `uv run python -c "from pathlib import Path as P; from trailer_to_campaign.catalog import load_film as l, default_source as d; print(d(l(P('films'),'demo-agent-327'),P('films')))"`
- [ ] Free check, **before your screen is shared**: mention the bot in the test channel with no Film named. The presenter should get "I couldn't tell which Film you mean". That reply lists every local Film, including unreleased ones, and so does a mistyped slug, so type `demo-agent-327` carefully on stage.
- [ ] Slack is open in the test channel, signed in as the presenter, with the text large enough to read from the back.
- [ ] The planted sentence below is on the clipboard.
- [ ] The backup recording is open, and you know what it shows (see "The backup recording").

## On stage

**The planted sentence.** This is the only text to paste into the post. Copy it to the clipboard before you start:

```
In the end, Agent 327 is captured and the villains plan to make him their next recruit.
```

| Step | Do | Say out loud (never paste this) |
|---|---|---|
| 1 | Mention the bot with `demo-agent-327` | "A Marketer starts a Run from Slack with a Film's name. This is *Agent 327*, an open movie by Blender Studio." |
| 2 | Wait for the Key Moments, about a minute | "The Watcher studies the trailer's frames and speech. Every Key Moment is checked against what it actually saw." |
| 3 | Approve each Key Moment | "Nothing moves on without a person's Approval." |
| 4 | Wait for the Social Posts, about a minute | "The Copywriter writes six posts in English and Vietnamese, and the Checker reviews each one. The Copywriter never sees the Spoiler List." |
| 5 | On Facebook English, click **Edit**, paste the planted sentence at the end of the post, and save | "Now I'll give away the Film's ending in this post, on purpose." |
| 6 | Wait about 10 seconds. The thread shows **"⚠️ The Checker flagged the edit"**. The private message must have a line starting **"⚠️ spoiler ending-capture"** | "That's the catch. Only Marketers see the Checker's reason, because it can describe the spoiler." |
| 7 | Click **Reject** | "Even a person's own edit gets checked. A Marketer can still overrule the Checker, but the record will say so." |
| 8 | Decide the other five posts | "Approve, edit or reject each one, and every decision is recorded." |

**Never approve the flagged edit.** The Run has no cancel (#38), so it has to reach the end.

**A flag alone isn't the catch.** The Checker also flags edits for other reasons. Only a private reason that names `ending-capture` is the catch.

**The edited post stays in the thread,** spoiler included, even after Reject. Only the Checker's reason is private. That's fine for this released Film, but don't try this on a Film whose Spoiler List is real.

**A note about length isn't the Checker.** If the post is long, the thread may also say "facebook posts are at most 500 characters". That comes from code, so keep to the Checker's reason. To avoid it, delete a sentence from the post as you paste.

If a post's header says "after 1 retries", the Checker already sent that post back once. It's worth a sentence, but it doesn't show the reason, so it isn't the catch this script relies on.

## If it goes wrong

| What happens | What to do |
|---|---|
| The edit isn't flagged, or its reason doesn't name `ending-capture` | Say so plainly, then play the recording. Never claim a catch that didn't happen |
| "Not re-checked" appears | Same: the Checker didn't run, so this isn't a catch |
| The app stops responding | Restart it once with the command above and mention the bot again. If it's still stuck, play the recording |
| Slack or the API is down | Play the recording |

**Whenever you play the current recording, say what it shows:** the Checker flagging an edit that isn't marketing copy, not the planted spoiler being caught.

The planted sentence was caught 5 out of 5 times when measured for T13, outside Slack. That's a small sample, the Checker can vary between runs (#38), and the sentence hasn't yet been tried in a full live Run, so rehearse it on Wed 11 Nov.

## After a Run

The Run Log can hold spoilers, so don't show it on stage. To confirm the catch afterwards:

```
jq -c 'select(.event=="post_checked" and .platform=="facebook" and .language=="en") | {verdict,spoiler_ids,rules}' runs/demo-agent-327/<time>/run.jsonl
```

The catch is the **last** line, with `"verdict":"reject"` and `spoiler_ids` of `["ending-capture"]`, because the edit is checked last. A last line with only `rules` is a brand objection, not the catch. An earlier line may be a catch the Copywriter's retry then fixed.

A Run costs about $0.45, inside the $2.00 Film Budget. Demo Runs stay under `runs/demo-agent-327/`, apart from the T12 result and the sample Film's Runs.

## The backup recording

Record the whole script with QuickTime (File > New Screen Recording), signed in as the presenter so the private reason shows. Keep the video outside git, because the repo doesn't hold footage. Note the time at which the flag appears, so you can jump to it on stage.

**What the current recording shows:** Run `20261104T035322Z`, with the flag at 5:30. That edit held a stray note, not the planted sentence, so its reason is `brand other`, not `spoiler ending-capture`. If you play it, say what it shows: the Checker flagging an edit that isn't marketing copy. Don't present it as the planted spoiler being caught. A new recording that follows the script would replace it.
