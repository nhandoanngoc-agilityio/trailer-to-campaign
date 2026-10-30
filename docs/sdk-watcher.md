# The Watcher on the Claude Agent SDK, compared with v0 (T11)

For the company talk (T13). v0 is the Watcher on our own hand-written tool loop (`agents/loop.py`); the SDK Watcher (`agents/watcher_sdk.py`) runs the same tools, prompts and grounding on the Claude Agent SDK, which is Claude Code packaged as a library. Choose one with `--watcher v0|sdk`; v0 stays the default.

## The bar

From T11's Analyze. Across the Eval Set, the SDK Watcher is "at least as good as v0" when:
- every Film completes
- it catches at least as many Input Traps
- it has no more grounding rejections

The Watcher's cost per Film is reported but doesn't decide. Planted Drafts never reach the Watcher, so they only show that the Checker didn't drift between the two runs.

## Results

Two `t2c eval` runs on 2026-10-30, one after the other: `--watcher v0` (`runs/_eval/20261030T080038Z/`) and `--watcher sdk` (`runs/_eval/20261030T080918Z/`). Both cover the four Blender Films; Song Lang isn't on that clone, so it was skipped in both.

| | v0 | SDK |
|---|---|---|
| Films completed | 4 of 4 | 4 of 4 |
| Input Traps caught | 1 of 3 | 2 of 3 |
| Grounding rejections (total) | 1 | 1 |
| Key Moments (total) | 34 | 35 |
| Watcher cost (total) | $0.7115 | $0.7605 (+7%) |
| Watcher model calls / tool calls | 22 / 92 | 22 / 87 |
| Planted Drafts caught (Checker) | 8 of 8 | 8 of 8 |
| Clean Drafts: false alarms | 0 of 4 | 0 of 4 |
| Eval Set cost (total) | $1.4057 | $1.6238 |

Per Film, the Watcher's own numbers (cost, model calls, grounding rejections, Key Moments):

| Film | v0 | SDK |
|---|---|---|
| Sprite Fright | $0.1991, 6, 1, 8 | $0.1802, 5, 0, 8 |
| Cosmos Laundromat | $0.1418, 5, 0, 7 | $0.1713, 5, 0, 8 |
| Spring | $0.1731, 5, 0, 9 | $0.1714, 5, 0, 10 |
| Charge | $0.1976, 6, 0, 10 | $0.2376, 7, 1, 9 |

**Verdict: the SDK Watcher meets the bar.**
- Every Film completed.
- It caught at least as many Input Traps.
- It had no more grounding rejections: one each, a frame placed outside its moment's time range, caught and fixed on the next try.

Read the extra Input Trap as "no worse", not "better". Spring's `the-thaw` was caught because the Copywriter wrote a post touching it and the Checker named it, and the Checker varies from run to run (#38). The Watcher's own numbers are close on every Film.

The SDK Watcher cost 7% more. Most of the gap is one Film (Charge: one more model call after its grounding rejection). The SDK's own plumbing adds about 600 tokens to the first prompt (about 3,000 against v0's 2,400). The Eval Set's total cost differs more than the Watcher's, because the Copywriter and the Checker vary between runs.

**In the SDK Runs' logs:**
- every response was charged after the fact at our prices
- there were no worst-case charges, no SDK estimates and no other models
- the largest prompt was 25,323 tokens, against the margin's 100,000
- the only tools called were the Watcher's four

A smoke run on Agent 327 (`t2c watch --watcher sdk`) cost $0.1835 and stopped right after the accepted submission, with no extra model call.

v0 stays the default (T11's Analyze): switching is a call to make after the talk.

## Same tools, same guardrails

The SDK wraps each of our tools. Every call still goes through `watcher.tool_gate`, the one function v0 uses too, so nothing that keeps the Watcher safe was copied or rewritten:

| Guardrail | v0 | SDK Watcher |
|---|---|---|
| Pinned to this Run's Film and trailer | `tool_gate` | `tool_gate` (same function) |
| Key Moments grounded in what it was shown, 3 tries | `tool_gate` + `Submissions` | same |
| Never offered the Spoiler List (ADR 0007) | given only the footage and catalog servers | same servers |
| No other tools | it has only the tools we list | `tools=[]` (no Bash, files or web), `setting_sources=[]` (nothing from `~/.claude` or CLAUDE.md), only the four Watcher tools allowed, and a `PreToolUse` hook that refuses any other tool by name |
| Stops when done | the loop returns on an accepted submission | a `PostToolUse` hook ends the session (the SDK catches exceptions from tools, so a failure is recorded and the session stopped) |
| Film Budget | reserves the worst case before each call (ADR 0005) | a spending cap from what's left minus one worst-case response, a check in our prices before each next request (budget and prompt size), and charges after each response at our prices (ADR 0009) |
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
- **Cost.** 7% more for the Watcher across the Eval Set. The SDK made no background model calls once they were turned off, and it cached the prompt on its own.
- **Testing.**
  - The SDK's in-process tool server is real MCP. The tests drive it through a fake CLI (`tests/fake_agent_sdk.py`), so the SDK's own input checks and content conversion run in every test, with no CLI started and nothing spent.
- **Dependency.** `claude-agent-sdk` ships the Claude Code CLI inside it (113 MB).
