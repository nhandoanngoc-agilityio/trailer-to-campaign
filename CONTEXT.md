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

**Frame**:
A still sampled from the Source Footage at a timestamp, on a fixed interval or at a scene change; what the Watcher sees instead of video.
_Avoid_: Screenshot, thumbnail, image

**Transcript**:
The timestamped speech heard in the Source Footage. Lines the speech model judged not to be speech are kept apart and are never dialogue.
_Avoid_: Subtitles, captions, script

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

**v0 Watcher**:
The Watcher on the project's own hand-written tool loop; the default.
_Avoid_: Old Watcher, legacy Watcher

**SDK Watcher**:
The Watcher on the Claude Agent SDK, with the v0 Watcher's tools, prompts and grounding; chosen with `--watcher sdk`.
_Avoid_: New Watcher, Watcher v1, Claude Code Watcher

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

**Grounded**:
Said of a Key Moment that the code has checked against what the Watcher was actually shown: frames it looked at, words heard in the Transcript (never unheard lines), and cast from Catalog Metadata. Only grounded Key Moments go on to the Copywriter.
_Avoid_: Verified, validated, fact-checked

**Traced**:
Said of a Social Post whose every factual claim cites its source, a Key Moment or a Catalog Metadata field, whose every quoted line is a Key Moment's quote word for word, and which holds no control character but a newline nor half an emoji, as checked by code. Only traced posts go on to the Checker.
_Avoid_: Sourced, fact-checked, verified

**Flagged**:
Said of a Social Post the Checker still rejects after the Copywriter's retries, or that couldn't be fully reviewed. It is kept, with the Checker's reasons, for a marketer to fix or drop at Approval, and never counts as passed.
_Avoid_: Failed, blocked, escalated

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

**Marketer**:
A member of the marketing team who has been named as able to give Approval. Only a Marketer's action counts as Approval; anyone else who can see a Run can follow it but cannot decide on its drafts.
_Avoid_: User, reviewer, approver

**Approval**:
A human marketer's explicit decision to accept, edit, or reject a Campaign Draft; nothing reaches the public without one.
_Avoid_: Review, sign-off, check

**Approval Rate**:
The share of Campaign Drafts approved with a Light Edit or none; the project's built-in quality measure.
_Avoid_: Accuracy, score

**Light Edit**:
A change the approving marketer marks as light; rewriting a Draft's hook is never light, however few words it touches.
_Avoid_: Minor change, tweak

**Measured Change**:
The share of a draft's words an edit changed, from 0% (unchanged) to 100% (rewritten); shown to the approving marketer and recorded beside their Light Edit mark as a check on it, never in place of it. A Light Edit over 30% is flagged for a second look.
_Avoid_: Edit distance, diff size

**Film Budget**:
The maximum model spend allowed for one Run; the Run stops when it is reached.
_Avoid_: Cost limit, quota

**Run**:
One pass of the agent team over one Film, from its trailer to the Approval of its Social Posts; the Approval of Key Moments is a pause inside the Run, however long it takes. Rework after a rejected draft stays in the same Run; starting the Film again (a new request, a re-run, an Eval Set run) starts a new Run. It ends completed, at the Film Budget, or in an error. An Eval Set Run ends at the Checker: no Approval is asked for or recorded.
_Avoid_: Job, execution, session

**Run Thread**:
The Slack thread, started by the request that began a Run, where that Run posts its progress and Campaign Drafts and where Marketers give Approval. One Run, one Run Thread: a new request, even inside an old thread, starts a new Run in a new Run Thread.
_Avoid_: Channel, conversation, campaign thread

**Run Log**:
The record a Run leaves of every model call, what each cost, and how the Run ended.
_Avoid_: Trace, audit log, history

**Eval Set**:
A fixed group of released Films, with the posts actually published for them and planted Traps, that every change to the agents is tested against. In slice 1 these are openly licensed Films, whose studio's own published posts stand in for the team's.
_Avoid_: Test data, benchmark

**Trap**:
A deliberate error planted in an Eval Set Film (a Spoiler List item, a wrong cast name) that the Checker must catch. It is planted either in the Film's inputs, where it is caught only if a post falls into it, or in a Planted Draft. An input Trap no post touches is not triggered: it counts as neither caught nor missed, and only a person reading the posts can tell it from a miss.
_Avoid_: Test case, gotcha

**Planted Draft**:
A hand-written Social Post carrying a Trap, given straight to the Checker so that whether the Trap is caught never depends on what the Copywriter happened to write.
_Avoid_: Trap post, bait draft, fake post

**Clean Draft**:
A hand-written Social Post with no Trap, given straight to the Checker beside the Planted Drafts; rejecting it is a false alarm.
_Avoid_: Control, negative case

**Answer Key**:
The human-owned record, for each Eval Set Film, of the posts actually published for it (by the team, or by the Film's studio when they stand in) and the Traps the Checker must catch. Agents read it and never change it.
_Avoid_: Expected output, fixtures, golden files

**Time Saved**:
The difference between campaign preparation time before and after the agent team, measured per Film, on the same deliverable both ways: the six Social Posts ready to publish.
_Avoid_: Efficiency, productivity

**Side-by-Side Trial**:
One upcoming Film prepared twice, the old way and with the agent team, by two marketers each timing their own active work, to measure Time Saved, Approval Rate and cost per Film. Only the one using the agent team acts as a Marketer.
_Avoid_: A/B test, benchmark, pilot
