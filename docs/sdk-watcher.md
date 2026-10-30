# The Watcher on the Claude Agent SDK, compared with v0 (T11)

For the company talk (T13). v0 is the Watcher on our own hand-written tool loop (`agents/loop.py`); the SDK Watcher (`agents/watcher_sdk.py`) runs the same tools, prompts and grounding on the Claude Agent SDK, which is Claude Code packaged as a library. Choose one with `--watcher v0|sdk`; v0 stays the default.

## The bar

From T11's Analyze. Across the Eval Set, the SDK Watcher is "at least as good as v0" when:
- every Film completes
- it catches at least as many Input Traps
- it has no more grounding rejections

The Watcher's cost per Film is reported but doesn't decide. Planted Drafts never reach the Watcher, so they only show that the Checker didn't drift between the two runs.

## Results

_To fill in from two `t2c eval` runs on the same day: `--watcher v0` and `--watcher sdk`._

| | v0 | SDK |
|---|---|---|
| Films completed | | |
| Input Traps caught | | |
| Grounding rejections (total) | | |
| Key Moments (total) | | |
| Watcher cost (total) | | |
| Watcher model calls (total) | | |
| Planted Drafts caught (Checker, unchanged) | | |
| Eval Set cost (total) | | |

Per Film: _from each `summary.json`, `films[].watcher`._

Verdict: _passes / misses the bar, and why._

## Same tools, same guardrails

The SDK wraps each of our tools. Every call still goes through `watcher.tool_gate`, the one function v0 uses too, so nothing that keeps the Watcher safe was copied or rewritten:

| Guardrail | v0 | SDK Watcher |
|---|---|---|
| Pinned to this Run's Film and trailer | `tool_gate` | `tool_gate` (same function) |
| Key Moments grounded in what it was shown, 3 tries | `tool_gate` + `Submissions` | same |
| Never offered the Spoiler List (ADR 0007) | given only the footage and catalog servers | same servers |
| No other tools | it has only the tools we list | `tools=[]` (no Bash, files or web), `setting_sources=[]` (nothing from `~/.claude` or CLAUDE.md), only the four Watcher tools allowed, and a `PreToolUse` hook that refuses any other tool by name |
| Stops when done | the loop returns on an accepted submission | a `PostToolUse` hook ends the session (the SDK catches exceptions from tools, so a failure is recorded and the session stopped) |
| Film Budget | reserves the worst case before each call (ADR 0005) | a spending cap from what's left minus one worst-case response, a prompt guard, and charges after each response at our prices (ADR 0009) |
| No double billing | API client `max_retries=0` | the CLI's retries off (`CLAUDE_CODE_MAX_RETRIES=0`, and no timeout retries) |
| Strict submission schema | `strict: true` on the API tool | not available through MCP tools; grounding still checks every field |

## What the SDK gave, and what it took

- **Code.**
  - `watcher_sdk.py` is 316 lines.
  - v0 has `watcher.py` (182 lines, now shared) plus the loop in `loop.py` (215 lines, shared with the Copywriter and the Checker).
  - The SDK removed our loop for this one agent, but took about as much code again to lock the harness down and meter it.
  - The harness is a coding agent first, so the work was mostly *removing* things: built-in tools, settings files, retries, background calls.
- **Control.**
  - v0 sees and counts each request before it is sent.
  - With the SDK, our code sees a response only once it has started, and a tool's exception never reaches us (the SDK shows it to the model). Hooks are the control points.
- **Cost.** _From the results: the SDK's own system plumbing, and any background calls it made._
- **Testing.**
  - The SDK's in-process tool server is real MCP. The tests drive it through a fake CLI (`tests/fake_agent_sdk.py`), so the SDK's own input checks and content conversion run in every test, with no CLI started and nothing spent.
- **Dependency.** `claude-agent-sdk` ships the Claude Code CLI inside it (113 MB).
