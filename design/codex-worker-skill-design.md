# `/codex-worker` skill design

Status: **implemented (thin spawn)**. `skills/codex-worker/SKILL.md` is the source of truth for shipped behavior. This file is the protocol. [`README.md`](../README.md) is the human index.

## 1. Purpose

The user wants local Codex from the parent chat without driving the TUI, and without a skill-owned snapshot/apply stack.

`/codex-worker` is the opt-in path: write a self-contained task, ask where Codex may write, spawn `codex exec` there. Isolation and merge are ordinary git.

## 2. One job

Spawn one local `codex exec` in a checkout the user named and accepted.

The skill does not snapshot, copy a private tree, inventory Sol vs user dirty files, apply a patch, or authorize.

## 3. Actors

| Actor | Job |
| --- | --- |
| **User** | Invoke. Choose the checkout (this tree, a new branch/worktree, or cancel). Review and merge with git. |
| **Parent agent** | Resolve model and effort. Write a task that Codex can run without this chat. Show git status. Wait. Spawn only after a named path and a yes. Report the session and current git status. |
| **Codex** | Run in that checkout. Leave the tree as it is when `exec` exits. |

## 4. Gate

Before `codex exec`, the parent inspects the invoke workspace (`rev-parse`, current branch, `status --short`, `worktree list`) and stops.

A vague "go ahead" is not enough. The parent needs a concrete checkout path and an explicit spawn yes. Create a branch or worktree only when the user asked and named it.

## 5. Spawn

```
codex exec -m <slug> -c model_reasoning_effort=<effort> -C <checkout> -s workspace-write -c approval_policy=never "$(cat <task-file>)"
```

`--skip-git-repo-check` only when that path is not a git repo. Do not widen to `danger-full-access`. Do not resume, queue, or attach a TUI.

After exit, show session id, last message, and `git -C <checkout> status --short`.

## 6. Model and effort

The parent resolves aliases from `<skill>/models.txt`, generated from `codex debug models` (listed visibility only). Default alias is `sol`. Default effort is `high` when listed, else the catalog default. Ambiguous family names and Cursor-shaped slugs are questions, not guesses.

## 7. Non-goals

- Skill-owned SNAP / WT1 / apply packet
- Nested-`.git` refuse (that existed to protect snapshot)
- File-auth enforcement in a runner
- A second `/codex-worker review` verb
