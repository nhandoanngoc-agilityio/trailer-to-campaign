# Slice 1 is a fixed Watcher → Copywriter → Checker pipeline, with no orchestrator

The first slice runs the three agents in a fixed order instead of letting an orchestrator agent decide which to run, even though orchestration is the more "agentic" design. A fixed pipeline is easier to debug and evaluate, and it gives a measured baseline (Approval Rate, Time Saved, cost) so that adding an Orchestrator and the Strategist in slice 2 can be judged against real numbers rather than assumed to be better.
