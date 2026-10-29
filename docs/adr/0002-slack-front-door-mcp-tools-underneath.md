# Slack is the front door; the tools are MCP servers underneath

Marketers use Trailer-to-Campaign from Slack (drop a trailer, get Campaign Drafts in a thread, give Approval there) instead of a dedicated web app, because the team already works in Slack and adoption is part of how the project is judged. The tools the agents call (catalog lookup, footage processing, Spoiler List) are built as MCP servers rather than code inside the Slack bot, so the same team can later be used from Claude Desktop or another client without rewriting them.

## Considered Options

- **Web app**: more control over the review screen, rejected for the MVP because it adds a place people must remember to go.
- **Claude Desktop only**: rejected as the main surface because not every marketer uses it; kept possible through the MCP servers.
