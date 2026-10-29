# Trailer-to-Campaign

A multimodal agent team that turns an indie film's footage into a launch campaign for the marketing team of an indie film streaming startup, with a human approving every output before it is used.

## Language

### Inputs

**Film**:
An independent title on the platform that is being prepared for launch.
_Avoid_: Movie, title, content

**Source Footage**:
The trailer or screener of a Film that the agents analyse; only footage the company has rights to use internally.
_Avoid_: Video, asset, media

**Catalog Metadata**:
The platform's stored facts about a Film: title, synopsis, genre, cast, runtime, language.
_Avoid_: Film info, details

**Spoiler List**:
The per-Film list, set by the Film's team, of plot points no Campaign Draft may reveal; anything shown in the official trailer is allowed unless it is on this list.
_Avoid_: Blocklist, restrictions

### Agents

**Watcher**:
The agent that studies a Film's Source Footage and produces its Key Moments.
_Avoid_: Vision agent, analyser

**Copywriter**:
The agent that turns approved Key Moments into Social Posts.
_Avoid_: Writer, creative agent

**Checker**:
The agent that reviews every Campaign Draft against the Spoiler List and the brand before it goes to Approval.
_Avoid_: Reviewer, validator

### Outputs

**Key Moment**:
A timestamped scene from the Source Footage with its mood and any quotable line; the factual base every other Campaign Draft is built on.
_Avoid_: Highlight, scene, clip

**Social Post**:
A ready-to-publish caption for one social platform, written in English or Vietnamese.
_Avoid_: Caption, copy, content

**Campaign**:
The full set of launch marketing work produced for one Film.
_Avoid_: Launch plan, project

**Campaign Draft**:
Any agent-produced piece of a Campaign that has not yet been approved.
_Avoid_: Output, result, suggestion

### Governance

**Approval**:
A human marketer's explicit decision to accept, edit, or reject a Campaign Draft; nothing reaches the public without one.
_Avoid_: Review, sign-off, check

**Approval Rate**:
The share of Campaign Drafts approved with a Light Edit or none; the project's built-in quality measure.
_Avoid_: Accuracy, score

**Light Edit**:
A change the approving marketer marks as light; rewriting a Draft's hook is never light, however few words it touches.
_Avoid_: Minor change, tweak

**Film Budget**:
The maximum model spend allowed for producing one Campaign; the run stops when it is reached.
_Avoid_: Cost limit, quota

**Eval Set**:
A fixed group of past Films, with the posts the team actually published and planted Traps, that every change to the agents is tested against.
_Avoid_: Test data, benchmark

**Trap**:
A deliberate error planted in an Eval Set Film (a Spoiler List item, a wrong cast name) that the Checker must catch.
_Avoid_: Test case, gotcha

**Answer Key**:
The human-owned record, for each Eval Set Film, of the posts the team actually published and the Traps the Checker must catch. Agents read it and never change it.
_Avoid_: Expected output, fixtures, golden files

**Time Saved**:
The difference between campaign preparation time before and after the agent team, measured per Film.
_Avoid_: Efficiency, productivity
