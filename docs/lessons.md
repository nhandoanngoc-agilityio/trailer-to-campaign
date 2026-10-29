# Lessons

One or two lines per ticket: what it taught about building agents for real. Newest at the bottom. These feed the "what we learned" part of the company talk.

| Ticket | Lesson |
|---|---|
| Setup | Writing the glossary and ADRs before any code made every later decision faster: most debates were already settled in writing. |
| Setup | Instructions guide an agent; hooks enforce. The rules that matter most (PRs only, no secrets, an Answer Key nobody can edit to pass) became tested hooks and settings, not just lines in CLAUDE.md. |
| Setup | CI for agents can fail silently. `/install-github-app` overwrote the tuned review workflow with the stock one, and the review action skips (yet shows green) whenever a PR's workflow file differs from `main`'s. Read the run log, not the check mark. |
