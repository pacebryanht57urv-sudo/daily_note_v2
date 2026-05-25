---
name: git-collaboration
description: Use when coordinating Git/GitHub work in this research-record repository, especially before starting experiments on different terminals, creating branches, committing, pushing, opening PRs, merging PRs, cleaning branches, or preventing collaborators from forgetting pull/branch/push steps.
---

# Git Collaboration Skill

## Goal

Keep `main` as the clean, reviewed research archive while allowing different terminals and collaborators to work safely in branches.

Codex should reduce Git memory burden for the user: remind, check, and explain the next smallest safe step instead of assuming the user remembers the workflow.

## Start Of Work Checklist

Before a new experiment, measurement, fabrication session, report, or rule update:

1. Check current branch and status.

```bash
git status --short --branch
```

2. If the task is not a tiny edit, start from latest `main`.

```bash
git switch main
git pull
git switch -c exp/YYYY-MM-DD-topic
```

Use branch prefixes by task type:

- `exp/YYYY-MM-DD-topic` for experiment, measurement, or fabrication sessions.
- `report/YYYY-MM-weekN` or `report/YYYY-MM-month` for reports.
- `rules/topic` for `AGENTS.md`, `README.md`, or `workspace/skills/` changes.

Tiny edits such as typo fixes or a small link correction may be committed directly on `main` after `git pull`.

## During Work

Use `git status --short --branch` whenever the user asks "what next", "where am I", or seems unsure.

Do not assume a file count from a UI is wrong: Git may collapse an untracked directory while commit panels count every file inside it.

For complete sessions and reports, prefer small meaningful commits. Do not commit raw large data, caches, secrets, temporary indexes, or instrument autosave state.

## Finish Work Checklist

When the user says a session/report/rule update is done:

```bash
git status --short --branch
git add <intended files>
git commit -m "Short clear message"
git push -u origin <branch>
```

Then create a PR on GitHub:

```text
<working-branch> -> main
```

Before merging, inspect `Files changed` and check:

- Only intended session/report/rule files are included.
- Images referenced by Markdown/HTML are present.
- Raw large data, caches, secrets, and temporary files are absent.
- `AGENTS.md` or skills were changed intentionally.

## After PR Merge

After GitHub says the PR was merged:

1. Delete the merged remote branch on GitHub if it is no longer needed.
2. Sync local `main`.

```bash
git switch main
git pull
```

3. Delete the local temporary branch.

```bash
git branch -d <branch>
```

Use `-d`, not `-D`, by default. `-d` refuses to delete unmerged work.

## When A PR Is Not Reviewed Yet

If a collaborator finished experiment 1 and opened a PR, but the PR is not merged yet:

- A new branch from `main` will not contain experiment 1 changes.
- If experiment 2 does not depend on experiment 1, branch from latest `main`.
- If experiment 2 depends on experiment 1's rule/skill/session changes, either:
  - split the shared rule/skill change into a separate PR and merge that first, or
  - branch experiment 2 from experiment 1's branch and clearly mark it as a stacked branch.

Do not merge an experiment branch into local `main` merely to reuse its changes unless the team explicitly wants to bypass PR review.

## Plain-Language Reminders

Use these simple explanations when the user is confused:

- Commit saves to the current local branch.
- Push uploads local commits to GitHub.
- PR asks GitHub to merge one branch into another.
- Merge makes the PR branch's changes enter `main`.
- GitHub branch deletion does not delete the local branch.
- Local `main` updates only after `git pull`.
