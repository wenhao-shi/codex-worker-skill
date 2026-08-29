# Codex skill

Opt-in Cursor skill that shells out to local Codex CLI (`codex exec -m gpt-5.6-sol`), then stops for parent and user review before any write to the user's tree.

Shipped as **v4**. Source of truth for behavior is `skills/codex/` (the installable skill). Protocol is in `design/`. This file is the index: what `/codex` is, how it is laid out, how to install it, and where the rest lives.

## What this is

`/codex` is a guest SWE path. The parent agent turns the chat into a self-contained task. A runner freezes the dirty checkout, starts Codex in a private tree, and waits. Codex may edit only that tree. When it stops, the parent presents Sol's delta. Only the parent or the user may authorize apply. The runner, not Codex, writes the user's files, and only after that authorization. Changes stay uncommitted and unpushed.

Billing is ChatGPT Plus through file auth (`auth_mode=chatgpt` in `$CODEX_HOME/auth.json`). The skill refuses API keys and does not scrape the OS Keychain.

v4 claims Cursor only.

## What this is not

- A pstack role, or an entry on an automatic model map. That would bill Cursor for Sol.
- Cursor `Task` with a `gpt-5.6-sol-*` slug. That bills Cursor.
- Support for Claude Code, OpenCode, or Pi.
- Codex TUI, `codex resume`, a queue, or a `/codex review` verb.
- `codex apply`, `codex mcp-server`, or `danger-full-access`.
- Automatic network, connectors, or a wider sandbox because the task "needs" it.
- `git init` in an unversioned user project, stash-as-snapshot, commit, or push.

## Layout

```
.
  README.md                 this file
  design/
    architecture.md         picture: actors, isolation, lifecycle
    codex-skill-design.md   protocol: decided rules, support limits, remaining evidence
  skills/codex/             installable skill (copy this folder)
    SKILL.md                parent steps; do not reconstruct the runner in chat
    agents/openai.yaml      implicit-invocation off
    references/prompt.md    RPC body Codex receives
    scripts/run.sh          thin wrapper
    scripts/run.py          runner (Python 3 stdlib)
```

Session trees and session records are separate:

| Path | Role |
| --- | --- |
| `<workspace>/scratch/codex/<id>/wt` | WT1. Codex may write here while the session is running. |
| `~/.codex/codex-skill/sessions/<id>/` | Metadata: phase, apply patch, fingerprints. Override with `CODEX_SKILL_HOME`. Codex must not rewrite this. |
| `~/.codex/codex-skill/acquire.lock` | Process-wide flock for ownership. |

## Install

Clone this repository, then copy the skill directory. Edit the copy in this repo, then recopy. Do not hand-edit `~/.cursor/skills/codex`.

```bash
git clone https://github.com/wenhao-shi/codex-worker-skill.git
mkdir -p ~/.cursor/skills
rm -rf ~/.cursor/skills/codex
cp -R codex-worker-skill/skills/codex ~/.cursor/skills/codex
```

Cursor reads `~/.cursor/skills`. Invocation is `/codex`. The skill sets `disable-model-invocation: true`, so the model cannot fire it on its own.

Requires a working `codex` on `PATH` and ChatGPT file auth as above.

## How to use it

In Cursor:

```
/codex [low|medium|high|xhigh|max] <task>
```

If the first token is not exactly one of those five words, the whole argument is the task and effort is `high`.

The parent must follow `skills/codex/SKILL.md`. That file is the step list: parse, write a resolved task Codex can run without the chat, call the runner, present the packet, wait for the user, then `authorize` / `reject` / `abandon`. Do not duplicate those steps here, and do not reconstruct snapshot, exec flags, or apply in an ad-hoc shell.

## Runner

Entry point: `skills/codex/scripts/run.sh`, which execs `scripts/run.py`. Stdlib only.

| Command | Job |
| --- | --- |
| `start --wt0 <path> --effort <level> --task-file <file>` | Acquire the apply-target lock, snapshot or copy, exec Codex, capture or report interrupted. |
| `status --session <id>` | Print phase and paths. |
| `packet --session <id>` | Print the review packet. Refuses if the WT1 tree changed after freeze. |
| `authorize --session <id> [--patch <subset>]` | Apply Sol work to WT0 worktree files only. `accepted` means apply finished and WT0 matches expected fingerprints. |
| `reject --session <id>` | Drop the candidate. No apply. |
| `abandon --session <id>` | Drop before apply, after writers have stopped. Forbidden from `integrating`. |
| `recover --session <id>` | Reconcile a stuck session. From `integrating`, inspect WT0; do not retry apply blindly. |
| `self-test` | Fixture checks. Does not call Codex. |

Subset authorize is whole-path against the frozen candidate. It cannot keep one of two edits inside the same file. An empty subset of a nonempty patch is a full drop: use `reject`.

## Design vs implementation

| Document | Authority |
| --- | --- |
| `skills/codex/scripts/run.py` and `SKILL.md` | Shipped behavior. Wins if docs disagree. |
| `design/codex-skill-design.md` | Protocol: requirements, lifecycle evidence, support limits. |
| `design/architecture.md` | Same protocol as a picture. |

v4 closed the implementation gate for every decided rule and for the recorded support limits. Remaining evidence (live billed auth, installed `setsid` descendants, installed `-c` override) does not block use. Writer-stop is process-group terminate plus a WT1 tree hash at apply time, not a claim that installed Codex has no `setsid` children.

## Support limits

Short form. Full record: `SKILL.md` and design §12.

- 50 MiB per included file, 1 GiB included tree: refuse.
- Gitignore is symmetric. Ignored Sol work is omitted from inventory.
- Nested extra `.git` on the source: refuse before snapshot or copy.
- Custom smudge, clean, or process filters: refuse, including dotted driver names and WT0 at apply.
- Apply patches use `--no-textconv`. Line-ending normalization can still occur.
- `SNAP` has no extra keep-alive ref. Default git GC grace only.
- No separate WT0 drift policy. Apply uses patch context plus expected fingerprints.
- File auth only. Keychain is not scraped.
- Symlink writes through an in-tree link that escape the apply target remain open.

## Self-test

From the repository root. Creates temporary git repos. Does not start Codex.

```bash
python3 skills/codex/scripts/run.py self-test
```

`skills/codex/scripts/run.sh self-test` is the same. Grant the process permission to write outside the workspace if the sandbox blocks `git init` under `/tmp`.
