---
paths:
  - "src/trailer_to_campaign/agents/checker.py"
  - "src/trailer_to_campaign/agents/review.py"
  - "src/trailer_to_campaign/agents/loop.py"
  - "src/trailer_to_campaign/approval.py"
  - "src/trailer_to_campaign/slack_approval.py"
  - "src/trailer_to_campaign/tools/spoilers.py"
---

# The Checker, the Spoiler List, the retry loop and Approval

This code decides whether a draft reaches Approval, or what it's checked against, so a mistake here reaches the public. Only the Checker may read the Spoiler List (ADR 0007). Check edge cases hard, and tell the human when higher effort or `ultrathink` is worth it. A PR that touches it is **risky**: ask for an ultrareview.

- A change to a gate or check has tests on both sides: invalid input is rejected and valid input still passes. Don't loosen a rule for a false positive without a test that the original bad case still fails.
- Nothing is posted and no Approval is recorded without a marketer's action in Slack.
- A Checker change alone is verified with `t2c eval --only drafts` (the `ticket-loop` skill, step 4).
