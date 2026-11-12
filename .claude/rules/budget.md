---
paths:
  - "src/trailer_to_campaign/budget.py"
  - "src/trailer_to_campaign/pricing.py"
  - "src/trailer_to_campaign/pipeline.py"
  - "src/trailer_to_campaign/agents/watcher_sdk.py"
---

# The Film Budget

A mistake here costs money without anyone noticing. Check edge cases hard (wrong numbers, off-by-one limits, a loop that never stops), and tell the human when higher effort or `ultrathink` is worth it. A PR that touches the Film Budget is **risky**: ask for an ultrareview.

- Every call to a paid model API goes through the Film Budget meter, reserved before the call.
- The one harness the meter can't reserve for call by call, the Agent SDK, is capped and charged after the fact (ADR 0009, `docs/adr/0009-sdk-watcher-budget-after-the-fact.md`).
- Local models (faster-whisper in the footage tool) cost nothing and are exempt.
- Any loop that calls a model needs a hard stop, and a test that it stops.
