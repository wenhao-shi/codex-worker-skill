---
name: codex
description: One-shot local Codex CLI (gpt-5.6-sol) edit, then parent/user review before apply.
disable-model-invocation: true
---

# Codex

Opt-in guest SWE path. Shell out to local `codex exec`. Stop for parent/user review. The runner owns isolation, the lock, inventory, and apply. You judge the task. You never apply.

v4 is Cursor only. There is no `/codex review` verb, no resume, no queue, no TUI.

## When this run is done

The user has a review packet, or a recorded failure, and WT0 was changed only if they authorized a runner apply that finished. Uncommitted. Not pushed.

## 1. Parse the invocation

Shape: `/codex [low|medium|high|xhigh|max] <task>`

- If the first token is exactly `low`, `medium`, `high`, `xhigh`, or `max`, that token is the effort. The rest is the task.
- Otherwise the whole argument string is the task. Effort is `high`.
- Exact first-token match only. `hihg fix the parser` is a task starting with `hihg`, not a misspelling of `high`.
- Empty task: stop. Do not start the runner.

Completion: effort is one of the five words, and the task string is non-empty.

## 2. Write a resolved task

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

## 3. Start the runner

Skill directory: the folder that contains this `SKILL.md`. Runner:

```
<skill>/scripts/run.sh start --wt0 <WT0> --effort <effort> --task-file <task-file>
```

WT0 is the workspace root the user invoked from, as a real path. Do not walk up to a parent git repo.

The runner:

- Requires ChatGPT/`auth_mode=chatgpt` from `$CODEX_HOME/auth.json`. Refuses `cli_auth_credentials_store` other than `file`. Binds the child to file auth. Refuses `OPENAI_API_KEY` and `CODEX_API_KEY` in the environment or in `auth.json`.
- Uses `codex exec -m gpt-5.6-sol -c model_reasoning_effort=<effort> --sandbox workspace-write -c approval_policy="never" -c sandbox_workspace_write.writable_roots=[] -c cli_auth_credentials_store="file"`.
- Refuses start if WT0 or runner metadata (`CODEX_SKILL_HOME` / session dir) is under `/tmp`/`TMPDIR`, if runner metadata would sit inside WT1, or if user `writable_roots` overlap WT0 or runner metadata. Does not pass `--add-dir` or `danger-full-access`.
- Snapshots git trees without moving `HEAD` or the real index, or copies then `git init` only inside the copy.
- Holds the apply-target lock until a proven terminal phase.

Do not reconstruct snapshot, worktree, exec flags, or apply in your own shell.

If start prints that user Codex config already has `network_access`, tell the user. That parse is best-effort. The skill did not grant it.

Completion: stdout names a session id and a phase of `reviewable` or `interrupted`, or start refused before a session owned the target.

## 4. If start failed or phase is not reviewable

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
| `reviewable` | Go to step 5. |

Completion: the user knows the phase, and you have not called `authorize` except from `reviewable`.

## 5. Present the packet

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

## 6. Wait for the user, then call the runner

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
- `scratch/codex/<id>/` is session checkout space. Ordinary source under `scratch/` is included. Metadata lives under `~/.codex/codex-skill/sessions/<id>/` so Codex cannot rewrite the apply patch.

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

## Install

Copy this directory to `~/.cursor/skills/codex` so `/codex` is invocable. Edit the copy in this repo, then recopy. Mechanical checks: `scripts/run.sh self-test` (no live Codex).
