# Codex worker skill

Opt-in, agent-agnostic skill that shells out to local Codex CLI (`codex exec`), then stops for parent and user review before any write to the user's tree.

Product name: **codex-worker-skill**. Invocation: **`/codex-worker`**. Source of truth for behavior is `skills/codex-worker/` (the installable skill). Protocol is in `design/`. This file is the index.

## What this is

`/codex-worker` is a guest SWE path. The parent agent turns the chat into a self-contained task and calls a runner. The runner freezes the dirty checkout, starts Codex in a private tree, and waits. Codex may edit only that tree. When it stops, the parent presents Sol's delta (Codex's edits vs the freeze). Only the parent or the user may authorize apply. The runner, not Codex, writes the user's files, and only after that authorization. Changes stay uncommitted and unpushed.

Billing is ChatGPT Plus through file auth (`auth_mode=chatgpt` in `$CODEX_HOME/auth.json`). The skill refuses API keys and does not scrape the OS Keychain.

The runner is host-blind. Install the same directory into each host's skill root.

## What this is not

- A pstack role, or an entry on an automatic model map. That would bill Cursor for Sol.
- Cursor `Task` with a `gpt-5.6-sol-*` slug. That bills Cursor.
- Codex TUI, `codex resume`, a queue, or a `/codex-worker review` verb.
- `codex apply`, `codex mcp-server`, or `danger-full-access`.
- Automatic network, connectors, or a wider sandbox because the task "needs" it.
- `git init` in an unversioned user project, stash-as-snapshot, commit, or push.

## Layout

```
.
  README.md
  LICENSE
  design/
    architecture.md
    codex-worker-skill-design.md
  skills/codex-worker/          installable skill (copy this folder)
    SKILL.md
    models.txt                  generated on first use; not in git
    agents/openai.yaml
    references/prompt.md
    scripts/run.sh
    scripts/run.py
```

| Path | Role |
| --- | --- |
| `<workspace>/scratch/codex/<id>/wt` | WT1. Codex may write here while the session is running. |
| `~/.codex/codex-worker-skill/sessions/<id>/` | Metadata. Override with `CODEX_SKILL_HOME`. Codex must not rewrite this. |
| `~/.codex/codex-worker-skill/acquire.lock` | Process-wide flock for ownership. |

## Install

Clone this repository, then copy `skills/codex-worker` so the folder name stays `codex-worker`:

```bash
git clone https://github.com/wenhao-shi/codex-worker-skill.git
mkdir -p ~/.cursor/skills ~/.claude/skills ~/.agents/skills
rm -rf ~/.cursor/skills/codex-worker ~/.claude/skills/codex-worker ~/.agents/skills/codex-worker
cp -R codex-worker-skill/skills/codex-worker ~/.cursor/skills/codex-worker
cp -R codex-worker-skill/skills/codex-worker ~/.claude/skills/codex-worker
cp -R codex-worker-skill/skills/codex-worker ~/.agents/skills/codex-worker
```

| Host | Skill root | Invocation |
| --- | --- | --- |
| Cursor | `~/.cursor/skills/codex-worker` | `/codex-worker` |
| Claude Code | `~/.claude/skills/codex-worker` | `/codex-worker` |
| Codex CLI | `~/.agents/skills/codex-worker` | `$codex-worker` |
| Pi | `~/.agents/skills/codex-worker` | `/skill:codex-worker` |
| OpenCode | prefers `~/.agents/skills` | native `skill` tool |

`disable-model-invocation` hides the skill from the model on Cursor, Claude Code, and Pi. Codex CLI uses `agents/openai.yaml`. OpenCode has no gate; the skill is model-visible there.

Edit the copy in this repo, then recopy. Requires `codex` on `PATH`, ChatGPT file auth, and `jq` for the first-use model catalog.

## How to use it

```
/codex-worker [model] [effort] <task>
```

Default model alias is `sol`. Default effort is `high` when that slug lists it. Ambiguous names (`gpt-5.6`, `5.6`) or unclear effort: the parent asks; it does not guess. The parent follows `skills/codex-worker/SKILL.md`.

On first use, or after a failed `codex exec`, the parent writes `<skill>/models.txt` from `codex debug models` (listed visibility only).

## Runner

Entry point: `skills/codex-worker/scripts/run.sh` → `scripts/run.py`. Stdlib only.

| Command | Job |
| --- | --- |
| `start --wt0 <path> --model <slug> --effort <level> --task-file <file>` | Lock, snapshot or copy, exec Codex, capture or report interrupted. |
| `status --session <id>` | Print phase and paths. |
| `packet --session <id>` | Print the review packet. |
| `authorize --session <id> [--patch <subset>]` | Apply Sol work to WT0 worktree files only. |
| `reject --session <id>` | Drop the candidate. |
| `abandon --session <id>` | Drop before apply, after writers have stopped. |
| `recover --session <id>` | Reconcile a stuck session. |
| `self-test` | Fixture checks. Does not call Codex. |

`--model` is a catalog slug (`gpt-5.6-sol`), not the alias `sol`. Cursor-shaped slugs (`gpt-5.6-sol-high`) are refused.

## Design vs implementation

| Document | Authority |
| --- | --- |
| `skills/codex-worker/scripts/run.py` and `SKILL.md` | Shipped behavior. Wins if docs disagree. |
| `design/codex-worker-skill-design.md` | Protocol. |
| `design/architecture.md` | Same protocol as a picture. |

## Support limits

Short form. Full record: `SKILL.md` and design §12.

- 50 MiB per included file, 1 GiB included tree: refuse.
- Gitignore is symmetric. Ignored Sol work is omitted from inventory.
- Nested extra `.git` on the source: refuse before snapshot or copy.
- Custom smudge, clean, or process filters: refuse.
- Apply patches use `--no-textconv`.
- File auth only. Keychain is not scraped.
- `models.txt` is a debug-catalog snapshot, not account entitlement.

## Self-test

From the repository root:

```bash
python3 skills/codex-worker/scripts/run.py self-test
```
