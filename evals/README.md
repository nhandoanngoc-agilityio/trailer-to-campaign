# evals/

The Eval Set: a fixed group of released Films that every change to an agent's prompt, tool or model is scored against before it merges (ADR 0008). `uv run t2c eval` runs it unattended.

```
evals/
├── eval_set.toml            the Films, by slug
├── films/<film>/film.toml   Catalog Metadata and the Spoiler List, in the same format as films/ (see films/README.md)
├── expected/<film>.toml     the Answer Key: written by a person only, never by Claude (guarded, see below)
└── drafts/                  Claude's candidates for a person to review and move in (gitignored)
```

The four Blender Films (CC-BY) are tracked in git: they are released, so their spoilers are public. `song-lang` is a real Film, so its folder and Answer Key are gitignored, and `t2c eval` skips it on a clone that doesn't have them.

## The Answer Key

One file per Film, checked strictly: an unknown key, a missing field or a `spoiler_id` that isn't on the Film's Spoiler List is refused before anything is spent.

It is guarded against accidental changes (T14):
- Claude's file tools are denied in `.claude/settings.json`.
- `.claude/hooks/block_forbidden.py` blocks the obvious shell writes: `>`, `cp`, `mv`, `tee`, `sed -i`, `rm`, `touch`, `git checkout`/`restore`.
- `.githooks/pre-commit` refuses any commit that changes it, however the files were written.
- The `answer-key` CI job shows red on any PR that changes it.

A person changing it on purpose:
1. Commits with `T2C_ANSWER_KEY_EDIT=1 git commit ...`. Claude's shell hook refuses that override, so only a person can use it.
2. Merges past the red CI job, knowing what changed.

```toml
[[published_posts]]   # what the Film's studio actually posted, shown beside the generated posts
platform = "instagram"
language = "en"
text = "..."
url = "https://..."
date = 2021-10-29

[[planted_drafts]]    # a hand-written post carrying a Trap, given straight to the Checker
id = "leak-ending"
trap = "spoiler"      # the Checker must name this Spoiler List item
spoiler_id = "..."
platform = "facebook" # facebook, instagram or tiktok
language = "en"       # en or vi
text = "..."

[[planted_drafts]]
id = "wrong-actor"
trap = "wrong_cast"   # the Checker must point at this name
wrong_name = "..."
platform = "tiktok"
language = "vi"
text = "..."

[[clean_drafts]]      # a hand-written post with no Trap: rejecting it is a false alarm
id = "clean"
platform = "instagram"
language = "en"
text = "..."

[[input_traps]]       # a Spoiler List item the trailer shows or says; caught if any verdict names it
spoiler_id = "..."
```

## Reading the report

`t2c eval` prints one line per Film and these totals, and writes `report.md` and `summary.json` under `runs/_eval/<time>/`:

- **Planted Drafts: caught X of Y**: deterministic, since the drafts don't depend on what the Copywriter writes. A drop against `main` blocks the PR. A spoiler Trap is caught when the Checker names its Spoiler List item; a wrong-cast Trap, when it calls the name an invented detail. The Checker has no cast list until #33, so it catches a wrong name only from what it already knows about a film: invented names and well-known films, but not a swap in a lesser-known one. Drafts the Checker failed to review are counted as not caught and shown as "not reviewed", so a timeout looks like what it is, not like a regression.
- **Input Traps: caught M of N**: an input Trap no verdict names is listed with the Film's posts, because only a person reading them can tell whether no post touched it or the Checker missed it.
- **Clean Drafts: false alarms K of N**: a Checker that rejects everything would catch every Trap, so a rise here is a regression too. N counts only Clean Drafts the Checker actually reviewed.
- **Cost**: the sum of the Films' Run Logs. Each Run has its own Film Budget.

`report.md` also shows each Film's generated posts beside its published posts, ungraded: whether they are as good is a marketer's call, not a model's.
