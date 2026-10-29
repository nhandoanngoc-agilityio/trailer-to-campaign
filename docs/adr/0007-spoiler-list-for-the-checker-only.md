# Only the Checker sees the Spoiler List, through its own MCP server

In slice 1 the Spoiler List reaches the Checker alone: it is served by a separate `spoilers` MCP server, while the Watcher and Copywriter are given only the `catalog` server. Showing the writers the list would let them avoid spoilers up front and save Checker retries, but it would put the spoiler text itself into the context of the agents that write posts, where it can be echoed, paraphrased or hinted at. Keeping it out also makes the Checker the real gate the Eval Set's Traps measure. Two servers rather than one tool switch, because any client that connects to a server sees all its tools, so "Checker only" could not otherwise be enforced.

## Considered Options

- **One Film server with both tools**: rejected, since every agent wired to it, and any client like Claude Desktop (ADR 0002), would see the Spoiler List.
- **Give the Copywriter the list as an "avoid" list**: rejected for slice 1. It may cut retries, but spoiler text would sit in the writer's context. Revisit with the retry numbers from the Eval Set (T8), not before.

## Consequences

The cost is Checker retries: a draft that touches a spoiler is written, caught and rewritten (up to the retry cap) rather than avoided. T6's retry loop and the Film Budget absorb this, and the Eval Set shows how much it costs.
