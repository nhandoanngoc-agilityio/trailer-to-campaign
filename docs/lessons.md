# Lessons

One or two lines per ticket: what it taught about building agents for real. Newest at the bottom. These feed the "what we learned" part of the company talk.

| Ticket | Lesson |
|---|---|
| Setup | Writing the glossary and ADRs before any code made every later decision faster: most debates were already settled in writing. |
| Setup | Instructions guide an agent; hooks enforce. The rules that matter most (PRs only, no secrets, an Answer Key nobody can edit to pass) became tested hooks and settings, not just lines in CLAUDE.md. |
| Setup | CI for agents can fail silently. `/install-github-app` overwrote the tuned review workflow with the stock one, and the review action skips (yet shows green) whenever a PR's workflow file differs from `main`'s. Read the run log, not the check mark. |
| T1 | A hard stop is only as hard as what it's told. Two reviews found the edges of the Film Budget meter (a token estimate just under a price tier, an error after the call was billed, a failed log write), and each became a stated contract with a test, not just a patch. |
| T2 | Real footage found what synthetic clips couldn't: Whisper invents dialogue over music. A guard that silently drops data trades one failure for another; setting the doubtful lines aside, marked never to quote, kept both the public and the material safe. |
| T3 | A strict loader is only strict where the data is present. The worst bug in T3 was an absence: a forgotten Spoiler List read as "no spoilers", so the Checker would pass everything. Make the empty case explicit (`spoilers = []`) and refuse the missing one. |
| T4 | Check what an agent says against what it was shown, in code. On its first real run the Watcher cited frames from outside its own Key Moment; a deterministic grounding check sent the problems back and it fixed them on the next try, for cents. A prompt can ask for grounding, but only a check can prove it. |
| T5 | The cheapest guardrail is a citation. Asking the Copywriter to list every claim with its source turned "every fact traces back" from a judgment into a check: code verified 32 citations and both quotes, and the human review became a table to read, not a hunt through prose. |
| T6 | A retry loop is a leak waiting to happen. To fix a spoiler the Copywriter needs to know what's wrong, but telling it why would put the Spoiler List in its context. Sending back only its own offending words, in a fixed sentence, was enough: the real Checker caught both a planted Trap and a real slip, and each was fixed in one retry without the writer ever seeing the list. |
