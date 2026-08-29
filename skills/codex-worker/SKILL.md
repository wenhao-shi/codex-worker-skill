---
name: codex-worker
description: One-shot local Codex CLI edit, then parent/user review before apply.
disable-model-invocation: true
---

# Codex worker

Opt-in guest SWE path. Shell out to local `codex exec`. Stop for parent/user review. The runner owns isolation, the lock, inventory, and apply. You judge the task. You never apply.

Product name: `codex-worker-skill`. Invocation: `/codex-worker` (Claude Code, Cursor), `$codex-worker` (Codex CLI), `/skill:codex-worker` (Pi), native `skill` tool (OpenCode). Agent-agnostic: same runner, copy this directory into the host skill root. There is no `/codex-worker review` verb, no resume, no queue, no TUI.

## When this run is done

The user has a review packet, or a recorded failure, and WT0 was changed only if they authorized a runner apply that finished. Uncommitted. Not pushed.

## 1. Refresh the model list if needed

Skill directory: the folder that contains this `SKILL.md`. Catalog file: `<skill>/models.txt`.

Refresh when either is true:

- The catalog file is missing (first use).
- A later `codex exec` fails (step 5). Do not refresh on runner refusals before exec (lock, filters, layout, overlap).

Refresh command (stdout only; warnings go to stderr):

```
codex debug models | jq -r '
  .models[]
  | select(.visibility == "list")
  | "\(.slug): \([.supported_reasoning_levels[].effort] | join(", ")) [default=\(.default_reasoning_level)]"
' > <skill>/models.txt
```

If that command fails, retry with `codex debug models --bundled` and the same `jq`. If both fail, stop and tell the user. Do not guess a catalog.

Each line is `slug: effort, effort, ... [default=level]`. That file is the only model/effort table you use this run. Do not search OpenAI docs to confirm slugs.

Completion: `models.txt` exists and has at least one slug line.

## 2. Parse the invocation

Shape: `/codex-worker [model] [effort] <task>`

Defaults if that part is omitted: model alias `sol`, effort `high` when `high` is in that model's list, otherwise that model's `default=` from `models.txt`.

Resolve **model** to a catalog slug before start:

- Exact slug match in `models.txt` wins.
- Unambiguous alias: `sol`, `terra`, `luna` (and forms like `5.6-sol`, `gpt-5.6-sol`) map to the unique matching slug.
- Family names (`5.6`, `gpt-5.6`, `5.4`) and any token that matches more than one slug: ask which listed slug. Do not default to Sol.
- Cursor-shaped names (`gpt-5.6-sol-high`): ask or refuse. Do not pass them as `-m`.
- If you cannot tell whether the first token is a model or the start of the task: ask. Do not guess.

Resolve **effort** against that slug's effort list on the same line:

- Exact token match only (`low`, `medium`, `high`, `xhigh`, `max`, `ultra`, or whatever that line lists).
- A token that looks like a misspelled effort (`hgih`) is not a guess and is not silently task text: ask.
- If omitted: use `high` when listed for that slug, else the line's `default=`. If neither is usable: ask.

The rest is the task. Empty task: stop. Do not start the runner.

Pass the runner the **canonical slug** from `models.txt`, not the alias.

Completion: a catalog slug, an effort listed for that slug, and a non-empty task string; or you have asked and stopped.

## 3. Write a resolved task

Codex does not receive this chat. "Do the approach we agreed on" is empty for Codex.

Write a self-contained task that includes every item that applies:

- Requested outcome
- Acceptance criteria you will use
- Relevant decisions and source paths
- Allowed and disallowed changes
- Known failures already tried
- Required validation

No chat pronouns. No "as above." If you cannot write that text, stop. Do not dump the transcript. Do not start the runner.

Completion: a task file on disk whose body can be executed with no parent chat.

## 4. Start the runner

```
<skill>/scripts/run.sh start --wt0 <WT0> --model <slug> --effort <effort> --task-file <task-file>
```

WT0 is the workspace root the user invoked from, as a real path. Do not walk up to a parent git repo.

The runner:

- Requires ChatGPT/`auth_mode=chatgpt` from `$CODEX_HOME/auth.json`. Refuses `cli_auth_credentials_store` other than `file`. Binds the child to file auth. Refuses `OPENAI_API_KEY` and `CODEX_API_KEY` in the environment or in `auth.json`.
- Uses `codex exec -m <slug> -c model_reasoning_effort=<effort> --sandbox workspace-write -c approval_policy="never" -c sandbox_workspace_write.writable_roots=[] -c cli_auth_credentials_store="file"`.
- Refuses start if WT0 or runner metadata (`CODEX_SKILL_HOME` / session dir) is under `/tmp`/`TMPDIR`, if runner metadata would sit inside WT1, or if user `writable_roots` overlap WT0 or runner metadata. Does not pass `--add-dir` or `danger-full-access`.
- Snapshots git trees without moving `HEAD` or the real index, or copies then `git init` only inside the copy.
- Holds the apply-target lock until a proven terminal phase.

Do not reconstruct snapshot, worktree, exec flags, or apply in your own shell.

If start prints that user Codex config already has `network_access`, tell the user. That parse is best-effort. The skill did not grant it.

Completion: stdout names a session id and a phase of `reviewable` or `interrupted`, or start refused before a session owned the target.

## 5. If start failed or phase is not reviewable

If stderr contains `codex-worker-skill: refresh-models` or `codex exec exited`, the `codex exec` attempt failed. Refresh `models.txt` (step 1) now. Report the error. Do not start a second exec on your own.

```
<skill>/scripts/run.sh status --session <id>
<skill>/scripts/run.sh recover --session <id>
```

| Phase | What you do |
| --- | --- |
| `failed_init` | Report the error. Stop. Target is free after cleanup. |
| `running` | Wait. Do not abandon. If the parent died, `recover`. A still-live runner with no published worker pid is launching, not dead. Dead runner plus no worker becomes `interrupted`. |
| `interrupted` | Capture or writer-stop failed. Do not authorize. Offer `abandon` only after writers have stopped. |
| `integrating` | Apply may have mutated WT0. `recover` returns to `reviewable` if the pre-apply baseline still matches. It records `accepted` only if WT0 matches the expected apply result recorded before mutation. Otherwise stay. No abandon. No second apply. No invented rollback. |
| `reviewable` | Go to step 6. |

Completion: the user knows the phase, and you have not called `authorize` except from `reviewable`.

## 6. Present the packet

```
<skill>/scripts/run.sh packet --session <id>
```

Also point at the artifact paths the packet lists (`apply.patch`, `review.diff`, `inventory.txt`, `reply.md`, and `user.patch` when git mode).

Split for the user:

- Actual edits (Sol work vs SNAP; the only delta that may apply)
- Unimplemented advice in the reply
- Validation Codex ran or skipped
- Leftover risk

User work vs `C0` is already in WT0. Do not apply `user.patch`.

Keep/drop unit is a group of related Sol edits, not every hunk. Default authorize is the whole frozen `apply.patch`. A subset file is allowed only if the runner can apply it to SNAP and every resulting tree entry (mode, type, object) matches the frozen candidate. Subset selection is whole-path: it cannot keep one of two edits inside the same file. If you cannot produce that subset, reject and start a tighter task.

Do not offer apply if `packet` or `authorize` reports the WT1 tree changed after freeze.

Completion: the user has seen the packet and the four-way split, and has chosen authorize, reject, or abandon.

## 7. Wait for the user, then call the runner

Do not treat Codex's reply, a zero exit, or your own judgment as accept.

```
<skill>/scripts/run.sh authorize --session <id>
<skill>/scripts/run.sh authorize --session <id> --patch <subset-patch>
<skill>/scripts/run.sh reject --session <id>
<skill>/scripts/run.sh abandon --session <id>
```

`authorize` applies to worktree files only. It does not commit, push, or touch the real index on purpose. `accepted` means authorized and apply finished and recorded, not "the user said yes."

After `accepted`, inspect WT0. Leave changes uncommitted. Report what landed.

Completion: a terminal phase (`accepted`, `rejected`, `abandoned`) or an honest `integrating` block, each produced by the runner.

## Guardrails

- One top-level Codex session per apply-target identity. Nested or overlapping targets refuse.
- Codex never accepts. You never set an accepted bit. The runner does, after apply.
- Denied sandbox ops are failures. Do not widen the sandbox to make the task work.
- Tasks that need more authority than `workspace-write` on WT1 refuse before exec.
- Never `git init` in an unversioned user project. Never push. Never stash as the snapshot.
- `scratch/codex/<id>/` is session checkout space. Ordinary source under `scratch/` is included. Metadata lives under `~/.codex/codex-worker-skill/sessions/<id>/` so Codex cannot rewrite the apply patch.

## Support limits

- 50 MiB per file, 1 GiB included tree: refuse.
- Gitignore exclusions are symmetric. Ignored Sol work is omitted from inventory.
- Nested extra `.git` directories: refuse on the source tree before copy or snapshot.
- Custom smudge, clean, or process filters: refuse before the git operation that would run them, including dotted driver names and WT0 at apply time.
- Apply patches are generated with `--no-textconv`. The review diff may still convert.
- Line-ending normalization can still occur. The apply artifact is a binary tree diff, not the review diff.
- SNAP is a commit without an extra keep-alive ref. Default git GC grace covers the session; no extra retention policy.
- Unexplained leftover `scratch/codex/*/wt` under WT0 or any ancestor directory blocks start, including copy-mode parents. The no-walk-up rule still applies only to SNAP's root.
- WT0 edits during review are not a separate drift policy. Apply uses patch context plus a pre-apply fingerprint of Sol paths. `accepted` requires those paths to match the expected post-apply fingerprints, computed by applying the authorized patch to a staging copy of the current WT0 layout. Staging uses `git rev-parse --git-path` for `info/attributes` and copies `core.attributesFile`, not only a literal `.git/info/attributes` path.
- Writer shutdown of Codex descendants is best-effort (`pgrep` on the process group). Enumeration errors are treated as not-stopped. Authorize re-hashes the WT1 included tree. A `setsid` child is not claimed to be caught. Live Codex descendant evidence from a billed run is still unproven. A worker this runner spawned is SIGTERM then SIGKILL on that process group, then waited on. If stop cannot be proved, the pid stays in the session record so abandon refuses.
- File auth only. `cli_auth_credentials_store` other than `file` is refused. The child is bound to file auth. Keychain is not scraped.
- `models.txt` is a debug-catalog snapshot (`visibility=list` only). It is not account entitlement. Hidden slugs are omitted on purpose.

## Install

Copy this directory so the folder name is `codex-worker`:

- Cursor: `~/.cursor/skills/codex-worker`
- Claude Code: `~/.claude/skills/codex-worker`
- Codex CLI, OpenCode, Pi: `~/.agents/skills/codex-worker`

Edit the copy in this repo, then recopy. Mechanical checks: `scripts/run.sh self-test` (no live Codex).
