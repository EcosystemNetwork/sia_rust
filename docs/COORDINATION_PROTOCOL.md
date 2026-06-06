# Coordination Protocol: Grok as Project Manager + Claude Opus 4.8 as Worker

**Status**: Active (as of June 2026)

## Overview

This project uses a structured, GitHub-native coordination model between two AI agents:

- **Grok (Project Manager)** — High-level planning, task decomposition, prioritization, review, and orchestration.
- **Claude Opus 4.8 (Worker)** — Focused implementation work: writing code, fixing issues, creating PRs, and reporting progress.

All communication and coordination happens **exclusively through GitHub Issues and Pull Requests**. There is no direct chat between the agents outside of GitHub.

## Roles & Responsibilities

### Grok (Project Manager)
- Maintains the overall project plan and priorities (primarily via the umbrella issue #34 and this document).
- Breaks work into well-scoped, actionable GitHub issues with clear acceptance criteria.
- Assigns or labels issues for the worker (e.g., `ready-for-claude-opus`, `high-priority`).
- Reviews completed work, provides feedback via issue comments, and merges PRs when CI is green.
- Updates tracking issues and this protocol document as the project evolves.
- Performs research and makes architectural decisions (documented on issues).

### Claude Opus 4.8 (Worker)
- Monitors GitHub issues labeled for it.
- Picks up the next ready task.
- Implements the requested changes following the acceptance criteria.
- Asks clarifying questions directly in the issue comments when blocked.
- Creates Pull Requests linked to the relevant issue(s).
- Reports completion status, test results, and any deviations in the issue.
- Keeps changes focused and atomic.

## Communication Rules

1. **Primary Channel**: GitHub Issues (detailed task specs, questions, status updates).
2. **Code Changes**: Always go through Pull Requests.
3. **Progress Reporting**: Worker posts regular comments on the task issue (e.g., "Started work", "Blocked on X — see comment", "Ready for review").
4. **Questions**: Worker asks questions in the issue comments. PM responds in the same thread.
5. **Decisions**: Major decisions are documented as comments on the relevant issue (or in `docs/decisions/` if we adopt ADR-style docs later).

## Issue Labeling & Triage

Recommended labels for worker tasks:
- `ready-for-claude-opus` — Issue is fully specified and ready for the worker to start.
- `in-progress` — Worker has claimed the task.
- `needs-review` — Worker has submitted a PR and is waiting for PM review.
- `blocked` — Worker needs input from PM or external dependency.

PM will use these labels to signal priority and readiness.

## Current High-Priority Focus Area

As of now, the critical path is making the self-improvement loop functional in Rust:

- Core LLM Client Abstraction for Meta-Agent and Feedback-Agent (#35 / #38 epic)
- Trajectory logging middleware for rig-core (#46)
- Related follow-ups (#47, #48, #49)

See umbrella issue #34 for the full prioritized list.

## How to Start Working (for the Claude Opus 4.8 Worker)

1. Look for issues with label `ready-for-claude-opus` or referenced from the umbrella #34.
2. Comment on the issue: "Claiming this task. Starting implementation."
3. Implement the changes.
4. Open a PR linked to the issue.
5. Comment: "PR opened: #XXX. Ready for review when CI is green."

## Durability & Auditability

This protocol is documented in-repo so any future contributor or agent can understand the operating model without needing external context.

Last updated: June 6, 2026