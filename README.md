# Codex worker skill

Opt-in, agent-agnostic skill that writes a self-contained task, asks where Codex should write, then runs local `codex exec` in that checkout.

Product name: **codex-worker-skill**. Invocation: **`/codex-worker`**. Source of truth for behavior is `skills/codex-worker/SKILL.md`.

## What this is

`/codex-worker` does one job: spawn Codex in a checkout the user accepted. The parent agent turns the chat into a self-contained task, shows git status, and waits. After an explicit yes and a named path, it runs `codex exec` there. The user owns branches, worktrees, review, and merge.

Isolation is ordinary git: make a branch or linked worktree first if you do not want Codex in the live checkout. The skill does not snapshot, apply, or authorize.

## What this is not

- A snapshot/apply runner, review packet, or `authorize` verb
- Codex TUI, `codex resume`, or `codex queue`
- A pstack role, or a Cursor `Task` with a `gpt-5.6-sol-*` slug
- `codex apply`, `codex mcp-server`, or `danger-full-access`

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
```

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

Edit the copy in this repo, then recopy. Requires `codex` on `PATH` and `jq` for the first-use model catalog.

## How to use it

```
/codex-worker [model] [effort] <task>
```

Default model alias is `sol`. Default effort is `high` when that slug lists it. Ambiguous names (`gpt-5.6`, `5.6`) or unclear effort: the parent asks; it does not guess.

The parent follows `skills/codex-worker/SKILL.md`: write a self-contained task, show git status, ask where to spawn, then run `codex exec` only after a named checkout and a yes.

On first use, or after a failed `codex exec`, the parent writes `<skill>/models.txt` from `codex debug models` (listed visibility only).

## Design vs implementation

| Document | Authority |
| --- | --- |
| `skills/codex-worker/SKILL.md` | Shipped behavior. Wins if docs disagree. |
| `design/codex-worker-skill-design.md` | Protocol. |
| `design/architecture.md` | Same protocol as a picture. |
