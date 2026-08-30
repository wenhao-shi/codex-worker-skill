---
name: codex-worker
description: Spawn a local one-shot Codex CLI run in a checkout the user accepted. Use when the user invokes /codex-worker.
disable-model-invocation: true
---

# Codex worker

One job: spawn local `codex exec` in a checkout the user accepted. The user owns branches, worktrees, review, and merge.

There is no snapshot, apply packet, or authorize step.

## 1. Refresh the model list if needed

Skill directory: the folder that contains this `SKILL.md`. Catalog: `<skill>/models.txt`.

Refresh when the catalog is missing, or when a later `codex exec` fails:

```
codex debug models | jq -r '
  .models[]
  | select(.visibility == "list")
  | "\(.slug): \([.supported_reasoning_levels[].effort] | join(", ")) [default=\(.default_reasoning_level)]"
' > <skill>/models.txt
```

If that fails, retry with `codex debug models --bundled` and the same `jq`. If both fail, stop. Do not guess a catalog.

`models.txt` is the only model/effort table this run.

Completion: `models.txt` exists and has at least one slug line.

## 2. Parse the invocation

Shape: `/codex-worker [model] [effort] <task>`

Defaults if omitted: alias `sol`; effort `high` when that slug lists it, otherwise the line's `default=`.

Resolve **model** to a catalog slug:

- Exact slug in `models.txt` wins.
- Unambiguous alias: `sol`, `terra`, `luna` (and forms like `5.6-sol`, `gpt-5.6-sol`) map to the unique matching slug.
- Family names (`5.6`, `gpt-5.6`) or any token that matches more than one slug: ask which listed slug.
- Cursor-shaped names (`gpt-5.6-sol-high`): ask or refuse. Do not pass them as `-m`.
- If you cannot tell whether the first token is a model or the start of the task: ask.

Resolve **effort** against that slug's list: exact token only (`low`, `medium`, `high`, `xhigh`, `max`, `ultra`, or whatever the line lists). A misspelled effort is a question, not task text.

The rest is the task. Empty task: stop.

Pass `codex exec` the **canonical slug**, not the alias.

Completion: a catalog slug, an effort listed for that slug, and a non-empty task; or you have asked and stopped.

## 3. Write a resolved task

Codex does not receive this chat. Write a self-contained task that includes every item that applies:

- Requested outcome
- Acceptance criteria
- Relevant decisions and source paths
- Allowed and disallowed changes
- Known failures already tried
- Required validation

No chat pronouns. No "as above." If you cannot write that text, stop.

Completion: a task file on disk whose body can be executed with no parent chat.

## 4. Gate: inspect git, then ask

Do this in the workspace the user invoked from, before any `codex exec`.

```
git rev-parse --show-toplevel --git-dir --git-common-dir
git branch --show-current
git status --short
git worktree list
```

If this is not a git checkout, say so and ask whether to spawn here anyway.

Present the facts: toplevel, current branch, clean or dirty (name the dirty paths), whether this worktree is the main checkout or a linked one, and the other worktrees.

Then **stop and ask**. Do not spawn yet. Offer these choices and wait for an explicit answer:

- Spawn in this checkout
- Create or switch to a branch or linked worktree first, then spawn there (do that git work only after they name the path and branch)
- Cancel

Use the host's structured question UI when it exists. Otherwise ask in chat. A vague "go ahead" is not enough: you need a concrete checkout path and a spawn yes.

Completion: the user named the checkout and said spawn, or cancelled. No `codex exec` before that.

## 5. Spawn

```
codex exec -m <slug> -c model_reasoning_effort=<effort> -C <checkout> -s workspace-write -c approval_policy=never "$(cat <task-file>)"
```

`<checkout>` is the path they accepted. Use `--skip-git-repo-check` only when that path is not a git repo.

Read the session id and last message from the command output. Then show:

- session id
- model and effort
- checkout path
- last message
- `git -C <checkout> status --short` and, if useful, `git -C <checkout> diff --stat`

Leave the tree as Codex left it. The user reviews and merges with git.

Completion: `codex exec` has exited, and the user has the session id plus current git status.

## Guardrails

- Spawn only after step 4 completes with a named checkout and a yes.
- Create a branch or worktree only when the user asked for one and named it.
- One top-level `codex exec` per invocation. Do not resume, queue, or attach a TUI.
- Denied sandbox is a failure. Do not widen to `danger-full-access` to make the task work.
