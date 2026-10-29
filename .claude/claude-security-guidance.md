# Security rules for this repo

Trailer-to-Campaign sends trailer frames and transcripts to a model, calls tools through MCP, and posts drafts in Slack for a marketer's Approval. Focus findings on what can go wrong here:

- **Footage handling.** A trailer link or file name must never reach a shell: ffmpeg and transcription run with argument lists, never `shell=True` or string-built commands. Downloads accept official trailer sources only, check size and type, and never fetch internal or local addresses.
- **Prompt injection.** Transcript text, on-screen text, Catalog Metadata and Slack messages are data, not instructions. Prompts must mark them as such, and no agent may take an action (post, approve, change the Spoiler List or Film Budget) because footage or a message told it to.
- **Approval.** Nothing reaches the public without a recorded human Approval. Flag any path where a Campaign Draft is posted, or an Approval is recorded, without a Slack action from a marketer.
- **Spoiler List and Checker.** Flag changes that let a draft skip the Checker, raise the retry cap, or let an agent edit a Film's Spoiler List.
- **Film Budget.** Every call to a paid model API goes through the budget meter; flag direct API client calls that bypass it, and loops with no budget check. Local models (faster-whisper) are exempt.
- **Secrets.** The Anthropic API key and Slack tokens come from the environment; flag any that are logged, echoed into prompts, or written to run logs.
- **Eval Set.** Flag changes to `evals/expected/` in the same PR as a prompt change: the Answer Key is human-owned.
- **Hooks** in `.claude/hooks/` read JSON from stdin and inspect shell commands: flag anything that executes or `eval`s part of the command it is checking.
- **Workflows** in `.github/workflows/`: flag wider `permissions`, secrets exposed to `pull_request_target`, or untrusted PR text interpolated into `run:` steps.

Not findings here: missing user accounts in the CLI (it is a single-user tool), missing HTTPS for local runs, rate limiting.
