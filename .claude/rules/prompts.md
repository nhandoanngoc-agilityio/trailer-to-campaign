---
paths:
  - "src/trailer_to_campaign/**/prompts/**"
---

# Agent prompts

- Prompts live in their own files next to the agent that uses them, so a prompt change shows up clearly in the diff and triggers the Eval Set.
- A prompt change can pass every unit test and still let a spoiler through. Verify runs the smallest Eval Set run that covers it (the `ticket-loop` skill, step 4), and a drop in Traps caught blocks the PR.
- A prompt that enforces a guardrail makes the PR **risky**: check it hard, tell the human when higher effort or `ultrathink` is worth it, and ask for an ultrareview.
- Footage, transcripts, Catalog Metadata and Slack messages go into prompts as data, never as instructions.
