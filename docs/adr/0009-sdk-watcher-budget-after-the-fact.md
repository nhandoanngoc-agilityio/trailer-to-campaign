# The SDK Watcher's Film Budget is a cap plus charges after the fact

Every model call in slice 1 goes through the Film Budget meter, which reserves a call's worst case before it is sent and refuses it if that could pass the limit (ADR 0005). The Watcher on the Claude Agent SDK (T11) can't be metered that way: the SDK runs its own loop inside the Claude Code CLI, and our code sees each response only after it has started. So for that Watcher we set the SDK's own spending cap (`max_budget_usd`) to what's left of the Film Budget minus one worst-case response (a 100,000-token prompt and a full 16,000-token reply: $1.12), because the SDK stops only after the response that crosses its cap. Before each tool result goes back to the SDK, and so before the next request, our code also checks two things in our own prices and stops the Run if either fails:
- the next prompt (this one, plus a full reply, plus this turn's tool results) can't pass the margin's 100,000 tokens; the largest Watcher prompt seen so far was about 26,000;
- what's left of the Film Budget is still at least the margin. The SDK's cap counts in its own price table, which could be low.

Each response is then charged to the meter at our prices from the usage it streams. Usage the stream never showed (a background call, say) is charged from the SDK's result. A model id we have no price for is charged at our dearest price. A response cut off, or a request in flight when the CLI dies, is charged its worst case, once. The CLI's own retries are turned off, so a call can't be billed twice and charged once. A reader should not "simplify" this to the SDK's own cost figure: it is an estimate from a price table bundled in the SDK, and our prices are the ones the Film Budget is kept in.

## Considered Options

- **Cap at exactly what's left**: rejected, because the response that crosses the cap still runs, so a Run could end up to one response over its Film Budget.
- **Check the meter in a hook before each tool call**: rejected, because a hook can't stop a model call that is already running, and it would be a second meter to keep in step with the first.

## Consequences

- The margin is held back from the Watcher: with the default $2.00 Film Budget it can spend about $0.88 before the SDK stops it, against about $0.21 for v0 on Agent 327. Below a $1.12 `--budget`, the SDK Watcher doesn't start at all.
- A Watcher whose next prompt could pass 100,000 tokens ends in an error rather than risking the margin. If real trailers need more, raise the limit and accept the larger margin with it.
- The SDK Watcher's `model_call` events say `charged: after` (or `worst_case`, `dearest_price`) and reserve nothing, where v0's say `actual` after a reservation.
- v0 stays the default Watcher (T11), so Runs keep the reserve-first meter unless `--watcher sdk` is chosen.
