# Trailer-to-Campaign gets a new repo instead of growing inside release-planner

The project lives in a new `trailer-to-campaign` repo rather than in the existing `release-planner` repo, even though ADR 0001 says it absorbs the planner. `release-planner` was built as a training exercise for a different shape (a planner, not a multimodal pipeline), so a clean repo with the glossary and ADRs from day one is clearer for both the build and the story. The planner's logic is ported in as the Strategist in slice 2.
