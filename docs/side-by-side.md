# Side by side: one real Film, two ways (T12)

The Side-by-Side Trial: one upcoming Film is prepared twice, so Time Saved, Approval Rate and cost per Film are measured on a real launch, not assumed. Marketer A prepares the posts the old way. Marketer B uses Trailer-to-Campaign and is the only one acting as a Marketer, which means giving Approval in Slack. Both follow this page, so both time the same work.

## Before trial day

1. **Pick the Film.** It must:
   - be upcoming on the platform, with its posts not yet written (otherwise A's time is fake);
   - have an official public trailer on YouTube or Vimeo, at most 10 minutes long. Trailers only, never a screener (ADR 0005);
   - launch before the talk (T13);
   - not be in the Eval Set (`evals/eval_set.toml`), because the prompts were tuned with those Films in view;
   - be in English or Vietnamese.

   If two Films qualify, take the one with more dialogue in its trailer.
2. **Write the Film's `film.toml`.**
   - The catalog facts come from the platform, and the Film's team writes the Spoiler List.
   - Both, with the trailer link, become `films/<film>/film.toml` (see `films/README.md` and `films/agent-327/film.toml`).
   - That folder is gitignored, because the Spoiler List is unreleased plot.
   - If the app's machine can't fetch the link, put the same public cut in `films/<film>/source/` and name it as `trailer_file` beside the `trailer` link, then re-check the cut's length. Never the full Film, if that isn't public (ADR 0005).
   - Neither marketer times this work: it is the same input on both sides.
3. **Choose the two marketers.**
   - A is whoever normally writes launch posts, so the baseline is real.
   - B is a colleague with similar experience.
   - B is not whoever set up Trailer-to-Campaign: knowing the system would make B faster than a real marketer. If that person has to take part, they are A, and the result says so.
4. **Set up Slack.**
   - Add B's Slack member id to `T2C_MARKETERS`.
   - Start `t2c slack` with the defaults: a $2.00 Film Budget and the v0 Watcher.
   - B practises once on `agent-327`, untimed, so the timed minutes aren't spent learning the buttons.

## Trial day

5. **Start A and B at the same time, in separate places.**
   - A stays out of the Slack channel. Neither sees the other's posts until both have stopped.
   - Both start with the same three things: the trailer link, the catalog facts and the Spoiler List.
6. **A works the old way,** using whatever A normally uses: watching the trailer, notes, a translator, even a general AI chat if that's the habit. A writes one line on what they used. The result shows it, so the baseline is plain to see.
7. **B mentions the app with the Film's slug** in the channel, then approves, edits or rejects each draft in the Run Thread.
   - **A rejected post:** B writes it by hand, and that time counts.
   - **Touch-ups after Approval:** these count too.
   - **A failed Run** (an error, or the Film Budget reached): B starts the Film again with a new mention, at most twice more. Every Run's cost counts. After 3 failed Runs the trial stops, and the problem is diagnosed before the trial is rescheduled.
8. **"Done" is the same on both sides:** six Social Posts ready to publish, for Facebook, Instagram and TikTok, each in English and Vietnamese.
   - Each marketer signs off their own: A spoiler-checks against the Spoiler List, and B approves in Slack.
   - If the team always has a reviewer, use one on both sides, never on one side only.

## What to time

- **Start** the stopwatch when you have the trailer link, the catalog facts and the Spoiler List in front of you.
- **Stop** it when your six posts are done (point 8).
- **Time active work only.**
  - Pause for breaks, meetings and lunch.
  - B also pauses while the agents run. Waiting for them isn't B's work; it shows in the Run Log, which the result puts beside the two times.
- **Write your minutes down the moment you stop,** with one line on anything unusual: an interruption, and for A, what they used.

## Then

9. **Send the two times in one message**, e.g. `A = 240, B = 55; A used ChatGPT for the Vietnamese`.
   - Say how A's time was got: by stopwatch, written down on stopping, or from memory if it wasn't.
   - Add which Run B used only if it wasn't the last one to finish.
   - No names are needed.
10. **The result:**

    ```
    uv run t2c result --film <film> --old-way <A's minutes> --new-way <B's minutes> --old-way-source stopwatch|memory [--run <folder>]
    ```

    `--old-way-source` is required, and the command refuses anything else, so the page never guesses. Use `stopwatch` only if A timed the work as above. Otherwise use `memory`: the page then says A's time is from memory, makes no stopwatch claim, and labels Time Saved as an estimate. That page takes B's minutes as given, because the flag is only about A. A `stopwatch` page says B was timed the same way as A, and nothing records that.

    This writes `runs/<film>/result.md`, plus `result.json` with the same numbers and the source:
    - **Time Saved:** A's minutes minus B's. If B took longer, the page says so. A time from memory makes it an estimate.
    - **Approval Rate:** from the Run whose Campaign was used. By default that's the last Run to finish that gave a Campaign (completed, with at least one Social Post approved). Otherwise `--run` names it.
    - **Approved over a flag:** how many Social Posts were approved though they were Flagged: the Checker or tracing objected, or the post couldn't be reviewed. Beside it, how many edits were approved with no re-check. Counts only. Read them with the Approval Rate: a post approved unedited or with a Light Edit over a flag still counts in it, because the Marketer decided. A heavy edit doesn't count in the rate, but does count here. A re-check that passed doesn't cover a fact the Marketer added: no code can trace that, and they're told so.
    - **Cost per Film:** every Run of the Film added up, re-runs and failed Runs included, since the Film Budget caps each Run, not the Film. Eval Set and `dummy-run` Runs are left out, because they aren't launch work.
    - Each Run's Run Log time, beside the two times, as a check on B's. Nothing checks A's.

The pages hold the Film's title and numbers only, never a post or a plot point. They stay gitignored next to the Run Logs. Once the team agrees, a copy goes to `docs/results/` for the talk, with the line on what A used. Until the Film has launched, the copy names it only by genre and language (e.g. "an upcoming Vietnamese drama"), never by title.
