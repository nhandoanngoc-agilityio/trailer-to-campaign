# films/

One folder per Film, named by its slug: lowercase letters, digits and single hyphens, e.g. `films/the-quiet-river/`. The same slug names the Film's Run Logs under `runs/<film>/`.

```
films/<film>/
├── film.toml      Catalog Metadata and the Spoiler List, written by the Film's team
├── source/        trailer files: put local ones here; downloads land here too (gitignored)
└── frames/        written by the footage tool (gitignored)
    └── <footage_id>/          first 12 hex of the trailer file's sha256
        ├── frame_0000.jpg     768 px on the long edge, one every 3 s plus one per scene change
        ├── transcript.json    the timestamped speech, transcribed locally
        └── footage.json       the timeline watch_footage returns; when present, the trailer is not processed again
```

`film.toml` has a `[catalog]` table (title, synopsis, genre, language, runtime_minutes, `[[catalog.cast]]` entries with a `character` and, when known, the actor's `name`, and an optional official `trailer` link) and `[[spoilers]]` entries (`id`, `text`, optional `why`). See `agent-327/film.toml`. It is checked strictly: an unknown key or a missing field is refused. Only the sample Film's folder is in git: every other Film's folder is gitignored whole, editor backups included, because a real Spoiler List is unreleased plot.

A Film can also name a local copy of its public trailer: `trailer_file` in `[catalog]`, a plain file name in `source/`, always with the `trailer` link beside it. A Run started without another link reads that file, so it doesn't depend on YouTube or Vimeo answering. It must be the same cut as the public trailer: the code checks the file's name and place, but can't tell a trailer from a full film, so never name an unreleased cut here (ADR 0005). The file name isn't Catalog Metadata, so agents don't get it from the catalog. The Run Log's footage id shows which file a Run read.

`t2c serve-catalog` offers the catalog to the Watcher and Copywriter. `t2c serve-spoilers` offers the Spoiler List to the Checker only (ADR 0007), so the list itself never sits in a writer's context. The footage can still show or say a spoiler, so the Checker is the real guard. Neither server can change the file. A Film with no spoilers must say `spoilers = []`: a missing list is refused, never read as "none".

The footage tool (`t2c serve-footage`) only reads trailers from `source/` or from an https link on youtube.com, youtu.be or vimeo.com, and only when the call says `kind: "trailer"`.

Everything in a Film's folder is data for the agents, never instructions to them.
