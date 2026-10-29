# Trailer-to-Campaign is the real project, built production-grade

We chose a multimodal agent team that turns a Film's Source Footage into a Campaign as the real project, because it lands every 2026 trend that is actually used in practice (multimodal input, MCP tools, evals and human-in-the-loop governance) in one thing the marketing team will use, and its story is "crossing the pilot-to-production gap", not "a demo". It absorbs the existing `release-planner` instead of running as a second, overlapping project: the planner's logic is ported in as the Strategist in slice 2 (see ADR 0006 for why the code lives in a new repo). Success for the MVP is measured by Time Saved and Approval Rate; business results such as click-through come later.

## Considered Options

- **MCP-native marketing ops platform**: kept as the way tools are built, rejected as the product, because infrastructure alone produces no visible result for the team.
- **Production-gap hardening of `release-planner` alone**: kept as the engineering discipline, rejected as the product, because it lacks a compelling user-facing outcome.
- **Localization agent for SEA expansion**: strong revenue story, deferred; it can become a later agent inside the same team.
- **A2A agent marketplace with festivals and distributors**: rejected, because no counterparty agents exist yet, so it cannot be a real project today.
