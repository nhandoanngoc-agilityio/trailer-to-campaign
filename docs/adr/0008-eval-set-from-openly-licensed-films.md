# The Eval Set is built from openly licensed Films, with their studios' posts standing in for the team's

The Eval Set (T8) uses four Blender Studio open movies (Sprite Fright, Cosmos Laundromat, Spring, Charge), released under CC-BY, plus one Vietnamese indie Film with a public official trailer (Song Lang). The Answer Key holds the posts each Film's studio or distributor actually published, standing in for posts our own team would have written. We chose this over the company's own past launches because those Films are rights-clean and already released: their trailers can go to a model under ADR 0005, and their Catalog Metadata and Spoiler Lists can be committed under `evals/`, so every clone and every CI run scores against the same Films. A reader should not take the Eval Set's scores as our team's Approval Rate: the published posts are someone else's taste, and the real Approval Rate comes from T12.

## Considered Options

- **The company's own past launches**: closest to the glossary's "posts the team actually published", but their Spoiler Lists are unreleased plot that must stay gitignored, so the Eval Set could not be shared or run in CI.
- **Only Blender Films**: rejected, because none is in Vietnamese, leaving Vietnamese Transcripts and Social Posts untested. Song Lang is the one exception to "openly licensed", and its folder stays gitignored like any real Film.
- **Agent 327 in the Eval Set**: rejected; it is the sample the prompts were built against, so it would flatter the scores.

## Consequences

- `films/README.md`'s rule that only the sample Film is in git does not apply to the Eval Set's Blender Films: their spoilers are already public.
- Traps are planted two ways (see `CONTEXT.md`): Planted Drafts give the deterministic "caught X of Y" that blocks a PR, while input Traps are reported as "caught M of N", with the posts for any not caught shown to a person, since only a reader can tell a Trap no post touched from a miss. Clean Drafts guard the other side: a Checker that rejects everything shows up as false alarms.
