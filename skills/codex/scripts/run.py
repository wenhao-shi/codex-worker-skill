#!/usr/bin/env python3
"""Codex skill runner. Mechanical protocol only. Does not accept work."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat as statmod
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any, Iterator

EFFORTS = ("low", "medium", "high", "xhigh", "max")
OWNING_PHASES = (
    "initializing",
    "running",
    "reviewable",
    "integrating",
    "interrupted",
)
TERMINAL_PHASES = ("accepted", "rejected", "abandoned", "failed_init")
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TREE_BYTES = 1 * 1024 * 1024 * 1024
TERM_GRACE_SEC = 1.0
KILL_GRACE_SEC = 1.0
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
GIT_IDENT = [
    "-c",
    "user.email=codex-skill@local",
    "-c",
    "user.name=codex-skill",
    "-c",
    "commit.gpgsign=false",
]
DIFF_APPLY = [
    "-c",
    "diff.renames=false",
    "-c",
    "core.autocrlf=false",
    "diff",
    "--binary",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
]
PROMPT_TEMPLATE = Path(__file__).resolve().parent.parent / "references" / "prompt.md"


def state_home() -> Path:
    override = os.environ.get("CODEX_SKILL_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "codex-skill"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def die(msg: str, code: int = 1) -> None:
    print(f"codex-skill: {msg}", file=sys.stderr)
    raise SystemExit(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def realpath(p: Path) -> Path:
    return Path(os.path.realpath(p))


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    text: bool = True,
    input_text: str | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=text,
        input=input_text if text else input_bytes,
    )
    if check and proc.returncode != 0:
        err_b = proc.stderr if not text else None
        err = (proc.stderr or proc.stdout or "") if text else (err_b or proc.stdout or b"")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        die(f"command failed ({proc.returncode}): {' '.join(argv)}\n{err.strip()}")
    return proc


def git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return run(["git", *args], cwd=cwd, env=env, check=check, text=text)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def session_dir(sid: str) -> Path:
    return state_home() / "sessions" / sid


def session_path(sid: str) -> Path:
    return session_dir(sid) / "session.json"


def load_session(sid: str) -> dict[str, Any]:
    path = session_path(sid)
    if not path.is_file():
        die(f"unknown session {sid}")
    return merge_worker_sidecar(load_json(path))


def save_session(sess: dict[str, Any]) -> None:
    save_json(session_path(sess["id"]), sess)


def worker_sidecar_path(sid: str) -> Path:
    return session_dir(sid) / "worker.json"


def merge_worker_sidecar(sess: dict[str, Any]) -> dict[str, Any]:
    path = worker_sidecar_path(str(sess.get("id") or ""))
    if not path.is_file():
        return sess
    try:
        extra = load_json(path)
    except (OSError, json.JSONDecodeError):
        return sess
    if extra.get("codex_pid") and not sess.get("codex_pid"):
        sess["codex_pid"] = extra["codex_pid"]
        sess["codex_pgid"] = extra.get("codex_pgid")
        sess["codex_launch"] = extra.get("codex_launch", sess.get("codex_launch"))
    if extra.get("codex_exit") is not None and sess.get("codex_exit") is None:
        sess["codex_exit"] = extra["codex_exit"]
        sess["codex_launch"] = extra.get("codex_launch", sess.get("codex_launch"))
    return sess


def persist_spawn_record(sess: dict[str, Any], fields: dict[str, Any]) -> None:
    sess.update(fields)
    sid = sess["id"]
    try:
        save_session(sess)
        worker_sidecar_path(sid).unlink(missing_ok=True)
    except OSError:
        payload = {
            "id": sid,
            "codex_pid": sess.get("codex_pid"),
            "codex_pgid": sess.get("codex_pgid"),
            "codex_launch": sess.get("codex_launch"),
            "codex_exit": sess.get("codex_exit"),
        }
        save_json(worker_sidecar_path(sid), payload)


def validate_session_id(sid: str) -> None:
    if not SESSION_ID_RE.match(sid) or "/" in sid or "\\" in sid or sid in (".", ".."):
        die(f"invalid session id {sid!r}; must be a single path component")


def refuse_existing_session(sid: str) -> None:
    if session_path(sid).is_file():
        die(f"init collision: session id {sid} already exists")


def all_sessions() -> list[dict[str, Any]]:
    root = state_home() / "sessions"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in root.glob("*/session.json"):
        try:
            data = load_json(p)
        except (OSError, json.JSONDecodeError):
            continue
        out.append(merge_worker_sidecar(data))
    return out


def paths_overlap(a: Path, b: Path) -> bool:
    a_r, b_r = realpath(a), realpath(b)
    if a_r == b_r:
        return True
    return a_r in b_r.parents or b_r in a_r.parents


def owning_sessions() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for sess in all_sessions():
        phase = sess.get("phase")
        if phase in OWNING_PHASES:
            found.append(sess)
            continue
        if phase in TERMINAL_PHASES and sess.get("cleanup_failed"):
            continue
        if phase not in TERMINAL_PHASES and phase is not None:
            found.append(sess)
    return found


def leftover_worktrees(wt0: Path) -> list[Path]:
    scratch = wt0 / "scratch" / "codex"
    if not scratch.is_dir():
        return []
    leftover: list[Path] = []
    for wt in scratch.glob("*/wt"):
        if wt.is_dir():
            leftover.append(realpath(wt))
    return leftover


def leftover_scan_roots(wt0: Path) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    cur = realpath(wt0)
    while True:
        if cur not in seen:
            seen.add(cur)
            roots.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return roots


def unexplained_leftover_blocks(wt0: Path) -> str | None:
    for base in leftover_scan_roots(wt0):
        if not paths_overlap(base, wt0):
            continue
        for wt1 in leftover_worktrees(base):
            sess = next(
                (s for s in all_sessions() if s.get("wt1") and realpath(Path(s["wt1"])) == wt1),
                None,
            )
            if sess is None:
                return (
                    f"unexplained leftover worktree {wt1} under {base} "
                    f"(no session record; overlaps {wt0})"
                )
            if sess.get("phase") in OWNING_PHASES:
                return f"leftover worktree {wt1} belongs to owning session {sess['id']}"
            if sess.get("phase") not in TERMINAL_PHASES:
                return f"leftover worktree {wt1} session {sess['id']} phase={sess.get('phase')}"
    return None


def warn_terminal_leftovers(wt0: Path) -> None:
    for base in leftover_scan_roots(wt0):
        for wt1 in leftover_worktrees(base):
            sess = next(
                (s for s in all_sessions() if s.get("wt1") and realpath(Path(s["wt1"])) == wt1),
                None,
            )
            if sess and sess.get("phase") in TERMINAL_PHASES:
                print(
                    f"codex-skill: warning: leftover WT1 {wt1} from terminal session {sess['id']}",
                    file=sys.stderr,
                )


def overlap_conflict(apply_target: Path) -> str | None:
    for sess in owning_sessions():
        other = Path(sess["apply_target"])
        if paths_overlap(apply_target, other):
            return (
                f"apply target {apply_target} overlaps session {sess['id']} "
                f"({other}, phase={sess['phase']})"
            )
    return None


def inside_session_checkout(path: Path) -> bool:
    parts = realpath(path).parts
    for i, part in enumerate(parts):
        if (
            part == "scratch"
            and i + 1 < len(parts)
            and parts[i + 1] == "codex"
            and "wt" in parts[i + 2 :]
        ):
            return True
    return False


_lock_depth = 0
_lock_fd: int | None = None


@contextmanager
def global_lock() -> Iterator[None]:
    global _lock_depth, _lock_fd
    if _lock_depth == 0:
        home = state_home()
        home.mkdir(parents=True, exist_ok=True)
        lock_path = home / "acquire.lock"
        _lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX)
    _lock_depth += 1
    try:
        yield
    finally:
        _lock_depth -= 1
        if _lock_depth == 0 and _lock_fd is not None:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
            _lock_fd = None


def mutate_session(
    sid: str,
    mutator: Any,
    *,
    require_phase: str | None = None,
    require_phases: tuple[str, ...] | None = None,
    allow_terminal: bool = False,
) -> dict[str, Any]:
    with global_lock():
        live = load_session(sid)
        original = live.get("phase")
        if original in TERMINAL_PHASES and not allow_terminal:
            die(f"refusing to write terminal session {sid} (phase={original})")
        if require_phase is not None and original != require_phase:
            die(f"session {sid} phase={original}; need {require_phase}")
        if require_phases is not None and original not in require_phases:
            die(f"session {sid} phase={original}; need one of {require_phases}")
        mutator(live)
        save_session(live)
        return live


def toml_value(text: str, key: str) -> str | None:
    match = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*"([^"]+)"', text)
    if match:
        return match.group(1)
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*'([^']+)'", text)
    if match:
        return match.group(1)
    return None


def require_chatgpt_auth() -> str:
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        if os.environ.get(key):
            die(f"{key} is set in the environment; refuse API-key billing")
    cfg = codex_home() / "config.toml"
    if cfg.is_file():
        store = toml_value(cfg.read_text(), "cli_auth_credentials_store")
        if store and store != "file":
            die(
                f"cli_auth_credentials_store={store!r}; "
                "runner only verifies file auth at $CODEX_HOME/auth.json"
            )
    auth_path = codex_home() / "auth.json"
    if not auth_path.is_file():
        die(f"missing {auth_path}; sign in with ChatGPT")
    auth = load_json(auth_path)
    mode = auth.get("auth_mode")
    if mode != "chatgpt":
        die(f"auth_mode={mode!r}; need ChatGPT subscription auth")
    if auth.get("OPENAI_API_KEY") or auth.get("CODEX_API_KEY"):
        die("auth.json contains an API key; refuse API-key billing")
    return str(mode)


def inherited_network_access() -> bool:
    cfg = codex_home() / "config.toml"
    if not cfg.is_file():
        return False
    return re.search(r"(?m)^\s*network_access\s*=\s*true\b", cfg.read_text()) is not None


def listed_writable_roots() -> list[Path]:
    cfg = codex_home() / "config.toml"
    if not cfg.is_file():
        return []
    text = cfg.read_text()
    if "writable_roots" not in text:
        return []
    match = re.search(r"(?ms)writable_roots\s*=\s*\[(.*?)\]", text)
    if not match:
        die("sandbox writable_roots is set but unparseable; refuse start")
    return [realpath(Path(os.path.expanduser(p))) for p in re.findall(r'"([^"]+)"', match.group(1))]


def sandbox_temp_roots() -> list[Path]:
    roots = [Path("/tmp")]
    for var in ("TMPDIR", "TMP", "TEMP"):
        raw = os.environ.get(var)
        if raw:
            roots.append(Path(raw))
    return [realpath(p) for p in roots]


def under_sandbox_temp(path: Path) -> Path | None:
    path_r = realpath(path)
    for tmp in sandbox_temp_roots():
        if path_r == tmp or tmp in path_r.parents:
            return tmp
    return None


def refuse_bad_writable_roots(wt0: Path, meta: Path, wt1: Path) -> None:
    wt0_r = realpath(wt0)
    meta_r = realpath(meta)
    home_r = realpath(state_home())
    wt1_r = realpath(wt1)
    for label, path in (
        ("WT0", wt0_r),
        ("runner metadata", meta_r),
        ("CODEX_SKILL_HOME", home_r),
    ):
        tmp = under_sandbox_temp(path)
        if tmp is not None:
            die(f"{label} {path} is under sandbox temp {tmp}; refuse start")
    if paths_overlap(home_r, wt1_r) or paths_overlap(meta_r, wt1_r):
        die("runner metadata would sit inside WT1; Codex could rewrite ownership records")
    forbidden = [wt0_r, home_r, meta_r, realpath(codex_home())]
    for root in listed_writable_roots():
        for item in forbidden:
            if paths_overlap(root, item):
                die(
                    f"sandbox writable_roots includes {root} overlapping {item}; "
                    "skill will not inherit extra write access to WT0 or runner metadata"
                )


def find_codex() -> Path:
    for candidate in (
        shutil.which("codex"),
        "/opt/homebrew/bin/codex",
        str(Path.home() / ".local" / "bin" / "codex"),
    ):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return Path(candidate)
    die("codex binary not found")
    raise AssertionError


def detect_git_mode(wt0: Path) -> tuple[bool, str]:
    inside = git(["rev-parse", "--is-inside-work-tree"], cwd=wt0, check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False, "no-git copy path"
    toplevel = realpath(Path(git(["rev-parse", "--show-toplevel"], cwd=wt0).stdout.strip()))
    if toplevel != realpath(wt0):
        if toplevel in realpath(wt0).parents:
            return False, f"git toplevel {toplevel} is an ancestor; copy path (no walk-up)"
        return False, f"git toplevel {toplevel} is not this workspace"
    head = git(["rev-parse", "--verify", "HEAD"], cwd=wt0, check=False)
    if head.returncode != 0:
        die("git repo has no HEAD commit; refuse until there is an initial commit")
    return True, "git worktree path"


def refuse_nested_git(root: Path, *, allow_root_git: bool) -> None:
    for gitdir in root.rglob(".git"):
        rel = gitdir.relative_to(root)
        if allow_root_git and rel == Path(".git"):
            continue
        if rel.parts[:2] == ("scratch", "codex"):
            continue
        die(f"nested git at {gitdir}; unsupported")


def check_file_size(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > MAX_FILE_BYTES:
        die(f"file exceeds {MAX_FILE_BYTES} bytes: {path} ({size})")


def defined_filters(cwd: Path, env: dict[str, str] | None = None) -> set[str]:
    proc = git(
        ["config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
        cwd=cwd,
        env=env,
        check=False,
    )
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        m = re.match(r"filter\.(.+)\.(?:clean|smudge|process)\s", line)
        if m:
            names.add(m.group(1))
    return names


def attr_filter_values(
    cwd: Path,
    paths: list[str],
    source: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    if not paths:
        return {}
    args = ["check-attr", "filter"]
    if source:
        args.extend(["--source", source])
    args.append("--stdin")
    proc = run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=False,
        input_text="\n".join(paths) + "\n",
    )
    if proc.returncode != 0:
        die(f"git check-attr failed\n{(proc.stderr or proc.stdout or '').strip()}")
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.rsplit(": filter: ", 1)
        if len(parts) != 2:
            continue
        path, value = parts
        if value not in ("unspecified", "unset"):
            found[path] = value
    return found


def refuse_custom_filters(
    cwd: Path, *, source: str | None = None, env: dict[str, str] | None = None
) -> None:
    drivers = defined_filters(cwd, env=env)
    if source:
        proc = git(["ls-tree", "-r", "--name-only", "-z", source], cwd=cwd, env=env, check=False)
        paths = [p for p in proc.stdout.split("\0") if p]
    else:
        proc = git(["ls-files", "-z"], cwd=cwd, env=env, check=False)
        paths = [p for p in proc.stdout.split("\0") if p]
        extra = git(["ls-files", "-o", "-z", "--exclude-standard"], cwd=cwd, env=env, check=False)
        paths.extend(p for p in extra.stdout.split("\0") if p)
    selected = attr_filter_values(cwd, paths, source=source, env=env)
    hits = {path: name for path, name in selected.items() if name in drivers}
    if hits:
        sample = next(iter(hits.items()))
        die(f"custom git filter {sample[1]!r} would run on {sample[0]}; unsupported")


def git_add_included(cwd: Path, env: dict[str, str]) -> None:
    exclude = cwd / "scratch" / "codex"
    args = ["add", "-A"]
    if exclude.exists():
        args.extend(["--", ".", ":!scratch/codex"])
    proc = git(args, cwd=cwd, env=env, check=False)
    err = f"{proc.stderr or ''}{proc.stdout or ''}"
    if proc.returncode != 0 and "ignored" in err.lower():
        git(["add", "-A"], cwd=cwd, env=env)
        git(["rm", "-r", "--cached", "--ignore-unmatch", "scratch/codex"], cwd=cwd, env=env, check=False)
        return
    if proc.returncode != 0:
        die(f"git add failed\n{err.strip()}")


def index_paths(cwd: Path, env: dict[str, str]) -> list[str]:
    proc = git(["ls-files", "-z"], cwd=cwd, env=env)
    return [p for p in proc.stdout.split("\0") if p]


def check_indexed_sizes(cwd: Path, env: dict[str, str]) -> None:
    total = 0
    for rel in index_paths(cwd, env):
        path = cwd / rel
        if path.is_file() and not path.is_symlink():
            check_file_size(path)
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if total > MAX_TREE_BYTES:
                die(f"tree exceeds {MAX_TREE_BYTES} bytes under {cwd}")


def nul_paths(text: str) -> list[str]:
    return [p for p in text.split("\0") if p]


def snapshot_git(wt0: Path, meta: Path, sid: str) -> tuple[str, str]:
    refuse_nested_git(wt0, allow_root_git=True)
    refuse_custom_filters(wt0)
    index = meta / "temp-index"
    if index.exists():
        index.unlink()
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    git(["read-tree", "HEAD"], cwd=wt0, env=env)
    git_add_included(wt0, env)
    check_indexed_sizes(wt0, env)
    refuse_custom_filters(wt0, env=env)
    tree = git(["write-tree"], cwd=wt0, env=env).stdout.strip()
    c0 = git(["rev-parse", "HEAD"], cwd=wt0).stdout.strip()
    snap = git(
        [*GIT_IDENT, "commit-tree", tree, "-p", c0, "-m", f"codex-skill snapshot {sid}"],
        cwd=wt0,
        env=env,
    ).stdout.strip()
    index.unlink(missing_ok=True)
    return c0, snap


def add_worktree(wt0: Path, wt1: Path, snap: str) -> None:
    refuse_custom_filters(wt0, source=snap)
    wt1.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "--detach", str(wt1), snap], cwd=wt0)


def remove_worktree(wt0: Path, wt1: Path) -> bool:
    if not wt1.exists():
        return True
    proc = git(["worktree", "remove", "--force", str(wt1)], cwd=wt0, check=False)
    if proc.returncode != 0 and wt1.exists():
        shutil.rmtree(wt1, ignore_errors=True)
        git(["worktree", "prune"], cwd=wt0, check=False)
    return not wt1.exists()


def snapshot_copy(wt0: Path, wt1: Path, meta: Path) -> str:
    refuse_nested_git(wt0, allow_root_git=False)
    if wt1.exists():
        shutil.rmtree(wt1)
    wt1.mkdir(parents=True)
    rsync = [
        "rsync",
        "-a",
        "--exclude",
        ".git",
        "--exclude",
        "scratch/codex",
        f"{wt0}/",
        f"{wt1}/",
    ]
    run(rsync)
    git(["init", "-b", "main"], cwd=wt1)
    refuse_custom_filters(wt1)
    git([*GIT_IDENT, "add", "-A"], cwd=wt1)
    refuse_custom_filters(wt1)
    git([*GIT_IDENT, "commit", "--allow-empty", "-m", "codex-skill SNAP"], cwd=wt1)
    git(["clean", "-fdx"], cwd=wt1, check=False)
    check_indexed_sizes(wt1, os.environ.copy())
    snap = git(["rev-parse", "HEAD"], cwd=wt1).stdout.strip()
    (meta / "copy-snap").write_text(snap + "\n")
    return snap


def tree_diff_names(cwd: Path, snap: str, tree: str, env: dict[str, str]) -> list[str]:
    proc = git(
        ["-c", "diff.renames=false", "diff", "--name-only", "-z", "--no-ext-diff", snap, tree],
        cwd=cwd,
        env=env,
    )
    return nul_paths(proc.stdout)


def write_apply_patch(cwd: Path, snap: str, tree: str, dest: Path, env: dict[str, str]) -> None:
    proc = git([*DIFF_APPLY, snap, tree], cwd=cwd, env=env, text=False)
    dest.write_bytes(proc.stdout or b"")


def capture_final_tree(sess: dict[str, Any]) -> tuple[str, Path, Path, list[str]]:
    wt1 = Path(sess["wt1"])
    meta = session_dir(sess["id"])
    snap = sess["snap"]
    refuse_custom_filters(wt1)
    index = meta / "capture-index"
    if index.exists():
        index.unlink()
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    git(["read-tree", "HEAD"], cwd=wt1, env=env)
    git_add_included(wt1, env)
    check_indexed_sizes(wt1, env)
    refuse_custom_filters(wt1, env=env)
    tree = git(["write-tree"], cwd=wt1, env=env).stdout.strip()
    apply_patch = meta / "apply.patch"
    review_diff = meta / "review.diff"
    inventory = meta / "inventory.txt"
    write_apply_patch(wt1, snap, tree, apply_patch, env)
    review = git(["diff", snap, tree], cwd=wt1, env=env)
    review_diff.write_text(review.stdout)
    names = tree_diff_names(wt1, snap, tree, env)
    inventory.write_text("\n".join(names) + ("\n" if names else ""))
    if bool(names) != bool(apply_patch.stat().st_size):
        die("inventory/patch emptiness mismatch")
    index.unlink(missing_ok=True)
    return tree, apply_patch, review_diff, names


def wt1_included_tree(sess: dict[str, Any]) -> str:
    wt1 = Path(sess["wt1"])
    meta = session_dir(sess["id"])
    refuse_custom_filters(wt1)
    index = meta / "freeze-index"
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    git(["read-tree", "HEAD"], cwd=wt1, env=env)
    git_add_included(wt1, env)
    tree = git(["write-tree"], cwd=wt1, env=env).stdout.strip()
    index.unlink(missing_ok=True)
    return tree


def assert_candidate_frozen(sess: dict[str, Any]) -> None:
    expected = sess.get("final_tree")
    if not expected:
        die("no frozen candidate tree")
    actual = wt1_included_tree(sess)
    if actual != expected:
        die("WT1 included tree changed after freeze; not reviewable")


def tree_entry(cwd: Path, tree: str, rel: str, env: dict[str, str]) -> str:
    proc = git(["ls-tree", "-z", tree, "--", rel], cwd=cwd, env=env, check=False)
    raw = proc.stdout.split("\0")[0] if proc.stdout else ""
    if not raw.strip():
        return "ABSENT"
    meta, sep, _path = raw.partition("\t")
    return meta if sep else raw


def assert_frozen_subset(sess: dict[str, Any], patch: Path, data: bytes) -> None:
    meta = session_dir(sess["id"])
    original = meta / "apply.patch"
    orig_sha = sess.get("apply_patch_sha256")
    if not original.is_file() or not orig_sha:
        die("missing frozen apply.patch fingerprint")
    if hashlib.sha256(original.read_bytes()).hexdigest() != orig_sha:
        die("apply.patch fingerprint mismatch; refusing authorize")
    if not data.strip() and original.stat().st_size:
        die("empty subset is a full drop; use reject")
    if hashlib.sha256(data).hexdigest() == orig_sha:
        return
    wt1 = Path(sess["wt1"])
    snap = sess["snap"]
    final = sess["final_tree"]
    inventory = set(sess.get("sol_paths") or [])
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(meta / "subset-index")
    git(["read-tree", snap], cwd=wt1, env=env)
    applied = git(["apply", "--cached", "--whitespace=nowarn", str(patch)], cwd=wt1, env=env, check=False)
    if applied.returncode != 0:
        die(f"subset patch does not apply to SNAP\n{applied.stderr}")
    new_tree = git(["write-tree"], cwd=wt1, env=env).stdout.strip()
    changed = tree_diff_names(wt1, snap, new_tree, env)
    extra = set(changed) - inventory
    if extra:
        die(f"subset introduces paths outside inventory: {sorted(extra)}")
    for rel in changed:
        if tree_entry(wt1, new_tree, rel, env) != tree_entry(wt1, final, rel, env):
            die(f"subset changes {rel} to an entry that is not in the frozen candidate")
    Path(env["GIT_INDEX_FILE"]).unlink(missing_ok=True)


def file_fingerprint(path: Path) -> str:
    try:
        st = path.lstat()
    except OSError:
        return "ABSENT"
    mode = statmod.S_IMODE(st.st_mode)
    if statmod.S_ISLNK(st.st_mode):
        return f"LINK:{mode}:{os.readlink(path)}"
    if statmod.S_ISDIR(st.st_mode):
        return f"DIR:{mode}"
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"FILE:{mode}:{digest.hexdigest()}"


def baseline_for_paths(wt0: Path, rels: list[str]) -> dict[str, Any]:
    return {"paths": {rel: file_fingerprint(wt0 / rel) for rel in rels}}


def baseline_changed(wt0: Path, baseline: dict[str, Any]) -> bool:
    paths = baseline.get("paths")
    if not isinstance(paths, dict):
        return True
    for rel, prior in paths.items():
        if file_fingerprint(wt0 / rel) != prior:
            return True
    return False


def staging_parent_is_dir(dest: Path, staging: Path) -> bool:
    parent = dest.parent
    if parent == staging:
        return True
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError, OSError):
        return parent.is_dir()
    return parent.is_dir()


def git_resolved_path(cwd: Path, spec: str) -> Path | None:
    proc = git(["rev-parse", "--git-path", spec], cwd=cwd, check=False)
    raw = proc.stdout.strip()
    if proc.returncode != 0 or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path


def copy_effective_git_attributes(wt0: Path, staging: Path) -> None:
    git(["init", "-b", "main"], cwd=staging)
    info = git_resolved_path(wt0, "info/attributes")
    if info is not None and info.is_file():
        dest = staging / ".git" / "info" / "attributes"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(info, dest)
    for key in ("core.autocrlf", "core.eol", "core.safecrlf"):
        proc = git(["config", "--get", key], cwd=wt0, check=False)
        val = proc.stdout.strip()
        if proc.returncode == 0 and val:
            git(["config", key, val], cwd=staging)
    attr = git(["config", "--get", "core.attributesFile"], cwd=wt0, check=False)
    raw = attr.stdout.strip()
    if attr.returncode == 0 and raw:
        src = Path(os.path.expanduser(raw))
        if not src.is_absolute():
            src = wt0 / src
        if src.is_file():
            dest = staging / ".git" / "info" / "codex-skill-attributesFile"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            git(["config", "core.attributesFile", str(dest)], cwd=staging)


def expected_after_apply(
    wt0: Path, patch: Path, rels: list[str], *, git_mode: bool
) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "s"
        staging.mkdir()
        if git_mode:
            copy_effective_git_attributes(wt0, staging)
        for src in wt0.rglob(".gitattributes"):
            rel = src.relative_to(wt0)
            if rel.parts[:2] == ("scratch", "codex"):
                continue
            dest = staging / rel
            if staging_parent_is_dir(dest, staging):
                shutil.copy2(src, dest, follow_symlinks=False)
        for rel in rels:
            src = wt0 / rel
            dest = staging / rel
            try:
                st = src.lstat()
            except OSError:
                continue
            if statmod.S_ISDIR(st.st_mode) and not statmod.S_ISLNK(st.st_mode):
                continue
            if not staging_parent_is_dir(dest, staging):
                continue
            shutil.copy2(src, dest, follow_symlinks=False)
        proc = apply_to_wt0(staging, patch, git_mode=git_mode)
        if not apply_ok(proc):
            die(f"cannot compute expected apply result\n{proc.stderr}")
        after: set[str] = set(rels)
        for path in staging.rglob("*"):
            rel = path.relative_to(staging)
            if ".git" in rel.parts:
                continue
            if path.is_file() or path.is_symlink():
                after.add(str(rel))
        return {rel: file_fingerprint(staging / rel) for rel in sorted(after)}


def matches_fingerprints(wt0: Path, expected: dict[str, str]) -> bool:
    return all(file_fingerprint(wt0 / rel) == prior for rel, prior in expected.items())


def apply_ok(proc: subprocess.CompletedProcess[Any]) -> bool:
    if proc.returncode != 0:
        return False
    err = proc.stderr or ""
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    return "Skipped patch" not in err


def apply_to_wt0(
    wt0: Path,
    patch: Path,
    *,
    git_mode: bool,
    check_only: bool = False,
    reverse: bool = False,
) -> subprocess.CompletedProcess[Any]:
    env = os.environ.copy()
    if not git_mode:
        env["GIT_DIR"] = str(wt0 / ".git-codex-skill-absent")
        env["GIT_WORK_TREE"] = str(wt0)
    args = ["apply"]
    if check_only:
        args.append("--check")
    if reverse:
        args.append("--reverse")
    args.append(str(patch))
    return git(args, cwd=wt0, env=env, check=False)


def write_prompt(sess: dict[str, Any], task: str) -> Path:
    template = Template(PROMPT_TEMPLATE.read_text())
    body = template.safe_substitute(
        task=task.rstrip() + "\n",
        git_mode="yes" if sess["git_mode"] else "no",
        c0=sess.get("c0") or "(none)",
        snap=sess["snap"],
        wt1=sess["wt1"],
        effort=sess["effort"],
    )
    path = session_dir(sess["id"]) / "prompt.md"
    path.write_text(body)
    return path


def write_review_packet(sess: dict[str, Any], paths: list[str]) -> Path:
    meta = session_dir(sess["id"])
    lines = [
        f"# Codex session {sess['id']}",
        "",
        f"- phase: {sess['phase']}",
        f"- WT0 / apply target: {sess['apply_target']}",
        f"- git_mode: {sess['git_mode']}",
        f"- C0: {sess.get('c0') or '(none)'}",
        f"- SNAP: {sess['snap']}",
        f"- effort: {sess['effort']}",
        f"- inherited network_access (best-effort parse): {sess.get('network_inherited')}",
        f"- auth_mode: {sess.get('auth_mode')}",
        "",
        "## Support limits",
        "",
        "- Fail closed above 50 MiB per file or 1 GiB included tree.",
        "- Gitignore exclusions are symmetric: ignored Sol work is omitted from inventory.",
        "- Nested extra `.git` directories are refused.",
        "- Custom smudge/clean/process filters are refused.",
        "- Apply patches use --no-textconv; the review diff may still convert.",
        "- WT0 drift during review is not a content policy; apply uses patch context plus a pre-apply path baseline.",
        "",
        "## Judgments",
        "",
        "1. Process finished: see `codex_exit` in session.json. This is not task success.",
        "2. Candidate complete enough to review: frozen tree hash plus apply.patch bytes.",
        "3. Task meets acceptance criteria: parent/user only. Codex does not accept.",
        "",
        "## Sol work paths (may apply after authorize)",
        "",
    ]
    if paths:
        lines.extend(f"- `{p}`" for p in paths)
    else:
        lines.append("- (no file changes in the final inventory)")
    lines += [
        "",
        "## Artifacts",
        "",
        f"- apply patch: `{meta / 'apply.patch'}`",
        f"- review diff: `{meta / 'review.diff'}`",
        f"- inventory: `{meta / 'inventory.txt'}`",
        f"- reply: `{meta / 'reply.md'}`",
    ]
    if sess["git_mode"]:
        lines.append(f"- user work (already in WT0, do not apply): `{meta / 'user.patch'}`")
    lines += [
        "",
        "## Parent review",
        "",
        "Split actual edits, unimplemented advice, validation, and leftover risk.",
        "Selectable unit is a group of related edits, not every hunk.",
        "Authorize uses the frozen apply.patch, or a subset whose resulting tree entries match that candidate.",
        "Do not offer apply if the WT1 included tree no longer matches the freeze hash.",
        "",
    ]
    path = meta / "review.md"
    path.write_text("\n".join(lines))
    return path


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pgid_has_live_members(pgid: int) -> bool | None:
    proc = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        return None
    for raw in proc.stdout.split():
        if raw.isdigit() and process_alive(int(raw)):
            return True
    return False


def launch_in_progress(sess: dict[str, Any]) -> bool:
    if sess.get("codex_pid") or sess.get("codex_pgid"):
        return False
    if sess.get("codex_exit") is not None:
        return False
    runner = sess.get("pid")
    if not (runner and process_alive(int(runner))):
        return False
    return sess.get("phase") in ("running", "interrupted") or sess.get("codex_launch") == "pending"


def terminate_spawned(proc: subprocess.Popen[Any]) -> bool:
    pgid: int | None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    if spawned_group_stopped(proc, pgid, TERM_GRACE_SEC):
        return True
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    return spawned_group_stopped(proc, pgid, KILL_GRACE_SEC)


def spawned_group_stopped(
    proc: subprocess.Popen[Any], pgid: int | None, timeout: float
) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is None or process_alive(proc.pid):
        return False
    if pgid is not None:
        live = pgid_has_live_members(pgid)
        if live is None or live:
            return False
    return True


def handle_unrecorded_worker(sess: dict[str, Any], proc: subprocess.Popen[Any], reason: str) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    stopped = terminate_spawned(proc)
    if stopped:
        persist_spawn_record(
            sess,
            {
                "codex_pid": None,
                "codex_pgid": None,
                "codex_launch": "failed",
                "codex_exit": -1,
                "spawn_error": reason,
            },
        )
        die(reason)
    persist_spawn_record(
        sess,
        {
            "codex_pid": proc.pid,
            "codex_pgid": pgid,
            "codex_launch": "started",
            "spawn_error": reason,
        },
    )
    die(f"{reason}; worker still live, pid recorded")


def writers_stopped(sess: dict[str, Any]) -> bool:
    if launch_in_progress(sess):
        return False
    pid = sess.get("codex_pid")
    if pid and process_alive(int(pid)):
        return False
    pgid = sess.get("codex_pgid")
    if pgid:
        live = pgid_has_live_members(int(pgid))
        if live is None or live:
            return False
    return sess.get("codex_exit") is not None or sess.get("phase") != "running"


def fail_start_session(sid: str, exc: BaseException | None = None) -> None:
    with global_lock():
        sess = load_session(sid)
        phase = sess.get("phase")
        if phase in TERMINAL_PHASES:
            return
        if phase == "initializing":
            sess["phase"] = "failed_init"
            if exc is not None:
                sess["error"] = str(exc)
            save_session(sess)
            cleanup_session(sess)
            return
        if phase in ("running", "reviewable"):
            sess["phase"] = "interrupted"
            if exc is not None:
                sess["error"] = str(exc)
            save_session(sess)


def cmd_start(args: argparse.Namespace) -> None:
    wt0 = realpath(Path(args.wt0))
    if not wt0.is_dir():
        die(f"WT0 is not a directory: {wt0}")
    if not args.task_file:
        die("empty task: need --task-file")
    task_path = Path(args.task_file)
    task = task_path.read_text()
    if not task.strip():
        die("empty task")
    effort = args.effort or "high"
    if effort not in EFFORTS:
        die(f"unknown effort {effort!r}")
    auth_mode = require_chatgpt_auth()
    git_mode, git_reason = detect_git_mode(wt0)
    if inside_session_checkout(wt0):
        die("WT0 is inside a session checkout; refuse nested session")

    sid = args.session or time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    validate_session_id(sid)
    meta = session_dir(sid)
    scratch = wt0 / "scratch" / "codex" / sid
    wt1 = scratch / "wt"
    refuse_bad_writable_roots(wt0, meta, wt1)

    with global_lock():
        refuse_existing_session(sid)
        conflict = overlap_conflict(wt0)
        if conflict:
            die(conflict)
        leftover = unexplained_leftover_blocks(wt0)
        if leftover:
            die(leftover)
        warn_terminal_leftovers(wt0)
        meta.mkdir(parents=True, exist_ok=True)
        sess = {
            "id": sid,
            "phase": "initializing",
            "wt0": str(wt0),
            "apply_target": str(wt0),
            "git_mode": git_mode,
            "git_reason": git_reason,
            "effort": effort,
            "scratch": str(scratch),
            "wt1": str(wt1),
            "created_at": now_iso(),
            "auth_mode": auth_mode,
            "network_inherited": inherited_network_access(),
            "cleanup_failed": False,
            "apply": {
                "started": False,
                "completed": False,
                "patch_sha256": "",
                "pre_baseline": {},
                "wt0_changed": None,
            },
        }
        save_session(sess)

    try:
        if git_mode:
            c0, snap = snapshot_git(wt0, meta, sid)

            def mut_git(s: dict[str, Any]) -> None:
                s["c0"] = c0
                s["snap"] = snap

            sess = mutate_session(sid, mut_git, require_phase="initializing")
            (meta / "origin-head").write_text(c0 + "\n")
            (meta / "snapshot").write_text(snap + "\n")
            user_patch = git([*DIFF_APPLY, c0, snap], cwd=wt0, text=False)
            (meta / "user.patch").write_bytes(user_patch.stdout or b"")
            scratch.mkdir(parents=True, exist_ok=True)
            add_worktree(wt0, wt1, snap)
        else:
            scratch.mkdir(parents=True, exist_ok=True)
            snap = snapshot_copy(wt0, wt1, meta)

            def mut_copy(s: dict[str, Any]) -> None:
                s["c0"] = None
                s["snap"] = snap
                s["git_mode"] = False

            sess = mutate_session(sid, mut_copy, require_phase="initializing")
            (meta / "snapshot").write_text(snap + "\n")
        shutil.copy2(task_path, meta / "task.md")
        sess = load_session(sid)
        prompt = write_prompt(sess, task)
        with global_lock():
            live = load_session(sid)
            if live.get("phase") != "initializing":
                die(f"session no longer initializing (phase={live.get('phase')})")
            live["phase"] = "running"
            live["pid"] = os.getpid()
            save_session(live)
            sess = live
        run_codex(sess, prompt)
    except SystemExit:
        fail_start_session(sid)
        raise
    except Exception as exc:
        fail_start_session(sid, exc)
        die(str(exc))

    sess = load_session(sid)
    if sess.get("phase") != "running":
        print(f"session {sid} phase={sess.get('phase')} after exec")
        sess = load_session(sid)
        print(f"git_mode {sess['git_mode']} ({sess.get('git_reason', '')})")
        return
    finish_after_exec(sess)
    sess = load_session(sid)
    print(f"git_mode {sess['git_mode']} ({sess.get('git_reason', '')})")
    if sess.get("network_inherited"):
        print(
            "inherited network_access=true from user Codex config "
            "(best-effort parse; not granted by this skill)"
        )


def run_codex(sess: dict[str, Any], prompt: Path) -> None:
    wt1 = Path(sess["wt1"])
    meta = session_dir(sess["id"])
    events = meta / "events.jsonl"
    last = meta / "last-message.txt"
    log = meta / "codex.stderr"
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    cmd = [
        str(find_codex()),
        "exec",
        "--cd",
        str(wt1),
        "--sandbox",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "-c",
        "sandbox_workspace_write.writable_roots=[]",
        "-c",
        'cli_auth_credentials_store="file"',
        "-m",
        "gpt-5.6-sol",
        "-c",
        f'model_reasoning_effort="{sess["effort"]}"',
        "--json",
        "-o",
        str(last),
        "-",
    ]
    sess["codex_cmd"] = cmd[:-1] + ["<prompt.md>"]
    mutate_session(sess["id"], lambda s: s.update({"codex_cmd": sess["codex_cmd"]}), require_phase="running")
    proc: subprocess.Popen[Any] | None = None
    with prompt.open() as stdin_f, events.open("w") as out_f, log.open("w") as err_f:
        with global_lock():
            live = load_session(sess["id"])
            if live.get("phase") != "running":
                die(f"session {sess['id']} phase={live.get('phase')}; not launching")
            live["codex_launch"] = "pending"
            save_session(live)
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=wt1,
                    stdin=stdin_f,
                    stdout=out_f,
                    stderr=err_f,
                    env=env,
                    start_new_session=True,
                )
            except Exception:
                live["codex_launch"] = "failed"
                save_session(live)
                raise
            live = load_session(sess["id"])
            if live.get("phase") != "running":
                handle_unrecorded_worker(
                    live, proc, f"session {sess['id']} left running before PID publish"
                )
            live["codex_pid"] = proc.pid
            try:
                live["codex_pgid"] = os.getpgid(proc.pid)
            except OSError:
                live["codex_pgid"] = proc.pid
            live["codex_launch"] = "started"
            try:
                save_session(live)
            except Exception:
                handle_unrecorded_worker(live, proc, "could not persist worker pid")
            sess = live
        code = proc.wait()

    def mut_exit(s: dict[str, Any]) -> None:
        s["codex_exit"] = code
        s["codex_pid"] = None

    try:
        sess = mutate_session(sess["id"], mut_exit, require_phase="running")
    except SystemExit:
        print(f"codex-skill: session {sess['id']} moved during exec; not rewriting phase")
        sess = load_session(sess["id"])
        return
    reply = last.read_text() if last.is_file() else ""
    if not reply and log.is_file():
        reply = log.read_text()[-20000:]
    (meta / "reply.md").write_text(reply)
    if code != 0:
        print(f"codex-skill: codex exec exited {code}", file=sys.stderr)


def finish_after_exec(sess: dict[str, Any]) -> None:
    sid = sess["id"]
    live = load_session(sid)
    if live.get("phase") != "running":
        return
    if not writers_stopped(live):

        def mut_live(s: dict[str, Any]) -> None:
            s["phase"] = "interrupted"
            s["capture_error"] = "writers still live after exec exit"

        live = mutate_session(sid, mut_live, require_phase="running")
        write_review_packet(live, [])
        print(f"session {sid} interrupted (writers still live)")
        print(f"session.json {session_path(sid)}")
        return
    try:
        tree, apply_patch, _review, paths = capture_final_tree(live)

        def mut_ok(s: dict[str, Any]) -> None:
            s["final_tree"] = tree
            s["sol_paths"] = paths
            s["apply_patch_sha256"] = hashlib.sha256(apply_patch.read_bytes()).hexdigest()
            s["phase"] = "reviewable"

        live = mutate_session(sid, mut_ok, require_phase="running")
        packet = write_review_packet(live, paths)
        print(f"session {sid} reviewable")
        print(f"packet {packet}")
        print(f"apply {apply_patch}")
    except SystemExit as exc:

        def mut_cap(s: dict[str, Any]) -> None:
            s["phase"] = "interrupted"
            s["capture_error"] = str(exc) or "capture failed"

        try:
            live = mutate_session(sid, mut_cap, require_phase="running")
        except SystemExit:
            print(f"session {sid} moved during capture; not rewriting")
            return
        write_review_packet(live, [])
        print(f"session {sid} interrupted (capture failed)")
        print(f"session.json {session_path(sid)}")
        return
    except Exception as exc:

        def mut_exc(s: dict[str, Any]) -> None:
            s["phase"] = "interrupted"
            s["capture_error"] = str(exc)

        try:
            live = mutate_session(sid, mut_exc, require_phase="running")
        except SystemExit:
            return
        write_review_packet(live, [])
        print(f"session {sid} interrupted (capture failed)")
        print(f"session.json {session_path(sid)}")
        return


def cmd_status(args: argparse.Namespace) -> None:
    if args.session:
        sess = load_session(args.session)
        print(json.dumps(sess, indent=2, sort_keys=True))
        return
    wt0 = realpath(Path(args.wt0)) if args.wt0 else realpath(Path.cwd())
    matches = [s for s in owning_sessions() if paths_overlap(wt0, Path(s["apply_target"]))]
    if not matches:
        print(json.dumps({"owning": []}, indent=2))
        return
    print(json.dumps({"owning": matches}, indent=2, sort_keys=True))


def cmd_packet(args: argparse.Namespace) -> None:
    sess = load_session(args.session)
    if sess.get("phase") == "reviewable":
        assert_candidate_frozen(sess)
    packet = session_dir(sess["id"]) / "review.md"
    if packet.is_file():
        sys.stdout.write(packet.read_text())
    else:
        die("no review.md; session is not reviewable")


def cmd_authorize(args: argparse.Namespace) -> None:
    with global_lock():
        sess = load_session(args.session)
        if sess["phase"] != "reviewable":
            die(f"authorize only from reviewable (phase={sess['phase']})")
        assert_candidate_frozen(sess)
        meta = session_dir(sess["id"])
        src_patch = Path(args.patch) if args.patch else meta / "apply.patch"
        if not src_patch.is_file():
            die(f"missing patch {src_patch}")
        wt0 = Path(sess["wt0"])
        refuse_custom_filters(Path(sess["wt1"]))
        refuse_custom_filters(wt0)
        data = src_patch.read_bytes()
        assert_frozen_subset(sess, src_patch, data)
        authorized = meta / "authorized.patch"
        authorized.write_bytes(data)
        rels = list(sess.get("sol_paths") or [])
        baseline = baseline_for_paths(wt0, rels)
        git_mode = bool(sess.get("git_mode"))
        expected = (
            expected_after_apply(wt0, authorized, rels, git_mode=git_mode) if data.strip() else {}
        )
        sess["phase"] = "integrating"
        sess["apply"] = {
            "started": True,
            "completed": False,
            "patch_sha256": hashlib.sha256(data).hexdigest(),
            "pre_baseline": baseline,
            "expected": expected,
            "wt0_changed": None,
            "started_at": now_iso(),
        }
        save_session(sess)
        if not data.strip():
            sess["phase"] = "accepted"
            sess["apply"]["completed"] = True
            sess["apply"]["wt0_changed"] = False
            sess["apply"]["completed_at"] = now_iso()
            save_session(sess)
            cleanup_session(sess)
            print(f"session {sess['id']} accepted (empty patch)")
            return
        check = apply_to_wt0(wt0, authorized, git_mode=git_mode, check_only=True)
        if not apply_ok(check):
            sess["apply"]["check_error"] = check.stderr
            if baseline_changed(wt0, baseline):
                sess["apply"]["wt0_changed"] = True
                save_session(sess)
                die("apply --check failed and WT0 may have changed; stay integrating")
            sess["phase"] = "reviewable"
            sess["apply"]["started"] = False
            sess["apply"]["wt0_changed"] = False
            save_session(sess)
            die(f"apply --check failed; returned to reviewable\n{check.stderr}")
        applied = apply_to_wt0(wt0, authorized, git_mode=git_mode)
        if not apply_ok(applied):
            changed = baseline_changed(wt0, baseline)
            sess["apply"]["apply_error"] = applied.stderr
            sess["apply"]["wt0_changed"] = changed
            save_session(sess)
            if changed:
                die("apply failed after WT0 mutation began; stay integrating; no rollback")
            sess["phase"] = "reviewable"
            sess["apply"]["started"] = False
            save_session(sess)
            die(f"apply failed; WT0 matches pre-apply baseline; returned to reviewable\n{applied.stderr}")
        if not matches_fingerprints(wt0, expected):
            changed = baseline_changed(wt0, baseline)
            sess["apply"]["apply_error"] = "apply exit 0 but WT0 does not match expected result"
            sess["apply"]["wt0_changed"] = changed
            save_session(sess)
            if changed:
                die("apply reported success but WT0 does not match expected result; stay integrating")
            sess["phase"] = "reviewable"
            sess["apply"]["started"] = False
            save_session(sess)
            die("apply reported success but WT0 does not match expected result; returned to reviewable")
        sess["phase"] = "accepted"
        sess["apply"]["completed"] = True
        sess["apply"]["wt0_changed"] = True
        sess["apply"]["completed_at"] = now_iso()
        save_session(sess)
        cleanup_session(sess)
        print(f"session {sess['id']} accepted")
        print("WT0 updated, still uncommitted. Not pushed.")


def cmd_reject(args: argparse.Namespace) -> None:
    with global_lock():
        sess = load_session(args.session)
        if sess["phase"] != "reviewable":
            die(f"reject only from reviewable (phase={sess['phase']})")
        if sess.get("apply", {}).get("started"):
            die("apply already started; reject is not valid")
        sess["phase"] = "rejected"
        save_session(sess)
        cleanup_session(sess)
        print(f"session {sess['id']} rejected")


def cmd_abandon(args: argparse.Namespace) -> None:
    with global_lock():
        sess = load_session(args.session)
        phase = sess["phase"]
        if phase == "running":
            die("abandon from running requires writers to stop first; wait or recover")
        if phase == "integrating":
            die("abandon from integrating is forbidden until WT0 outcome is known")
        if phase not in ("initializing", "interrupted", "reviewable"):
            die(f"cannot abandon from {phase}")
        if sess.get("apply", {}).get("started"):
            die("apply already started")
        if phase in ("interrupted", "reviewable") and not writers_stopped(sess):
            die("abandon requires writers to stop first; wait or recover")
        sess["phase"] = "abandoned"
        save_session(sess)
        cleanup_session(sess)
        print(f"session {sess['id']} abandoned")


def cmd_recover(args: argparse.Namespace) -> None:
    with global_lock():
        sess = load_session(args.session)
        if sess["phase"] == "running" and launch_in_progress(sess):
            print(json.dumps({"id": sess["id"], "phase": "running", "launch": "pending"}))
            return
        pid = sess.get("codex_pid")
        alive = bool(pid) and process_alive(int(pid))
        pgid = sess.get("codex_pgid")
        group_state = pgid_has_live_members(int(pgid)) if pgid else False
        group_alive = group_state is None or group_state is True
        if sess["phase"] == "running" and not alive and not group_alive:
            sess["codex_pid"] = None
            sess["codex_exit"] = sess.get("codex_exit", -1)
            sess["phase"] = "interrupted"
            save_session(sess)
            print(f"session {sess['id']} marked interrupted (pid dead)")
            return
        if sess["phase"] == "integrating":
            wt0 = Path(sess["wt0"])
            baseline = sess.get("apply", {}).get("pre_baseline") or {}
            changed = baseline_changed(wt0, baseline) if baseline else None
            sess["apply"]["wt0_changed"] = changed
            expected = sess.get("apply", {}).get("expected") or {}
            if changed is False:
                sess["phase"] = "reviewable"
                sess["apply"]["started"] = False
                save_session(sess)
                print(f"session {sess['id']} returned to reviewable (pre-apply baseline matches)")
                return
            if expected and matches_fingerprints(wt0, expected):
                sess["phase"] = "accepted"
                sess["apply"]["completed"] = True
                sess["apply"]["completed_at"] = now_iso()
                save_session(sess)
                cleanup_session(sess)
                print(f"session {sess['id']} accepted (recover: WT0 matches expected apply result)")
                return
            save_session(sess)
            print(json.dumps({"id": sess["id"], "phase": "integrating", "wt0_changed": changed}, indent=2))
            print("no automatic rollback; stay integrating until outcome is known")
            return
        print(json.dumps({"id": sess["id"], "phase": sess["phase"]}, indent=2))


def cleanup_session(sess: dict[str, Any]) -> None:
    wt0 = Path(sess["wt0"])
    wt1 = Path(sess["wt1"])
    ok = True
    if sess.get("git_mode"):
        ok = remove_worktree(wt0, wt1)
    elif wt1.exists():
        shutil.rmtree(wt1, ignore_errors=True)
        ok = not wt1.exists()
    scratch = Path(sess["scratch"])
    if scratch.is_dir() and not any(scratch.rglob("*")):
        shutil.rmtree(scratch, ignore_errors=True)
    sess["cleanup_failed"] = not ok
    save_session(sess)


def cmd_self_test(_args: argparse.Namespace) -> None:
    failures = 0

    def check(name: str, cond: bool) -> None:
        nonlocal failures
        if cond:
            print(f"ok  {name}")
        else:
            print(f"FAIL {name}")
            failures += 1

    check("overlap equal", paths_overlap(Path("/tmp/a"), Path("/tmp/a")))
    check("overlap nested", paths_overlap(Path("/tmp/proj"), Path("/tmp/proj/pkg")))
    check("no overlap sibling prefix", not paths_overlap(Path("/tmp/project"), Path("/tmp/project-extra")))
    check("nested checkout", inside_session_checkout(Path("/tmp/p/scratch/codex/s1/wt")))
    check("not nested scratch source", not inside_session_checkout(Path("/tmp/p/scratch/source")))

    try:
        validate_session_id("../x")
        check("reject parent id", False)
    except SystemExit:
        check("reject parent id", True)
    try:
        validate_session_id("a/b")
        check("reject slash id", False)
    except SystemExit:
        check("reject slash id", True)
    validate_session_id("ok-id_1")
    check("accept normal id", True)

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CODEX_SKILL_HOME"] = str(Path(tmp) / "state")
        root = Path(tmp) / "repo"
        root.mkdir()
        git(["init", "-b", "main"], cwd=root)
        (root / "feature_0").write_text("line1\nline2\nline3\n")
        (root / "old.txt").write_text("rename-me\n")
        (root / "space name.txt").write_text("spaced\n")
        (root / "scratch").mkdir()
        (root / "scratch" / "source.txt").write_text("keep-scratch-source\n")
        git(["add", "feature_0", "old.txt", "space name.txt", "scratch/source.txt"], cwd=root)
        git([*GIT_IDENT, "commit", "-m", "base"], cwd=root)
        (root / "feature_0").write_text("line1\nstaged\nline3\n")
        git(["add", "feature_0"], cwd=root)
        (root / "feature_0").write_text("line1\nstaged\nline3\nunstaged\n")
        (root / "feature_1").write_text("untracked\n")
        index_before = git(["show", ":feature_0"], cwd=root).stdout
        head_before = git(["rev-parse", "HEAD"], cwd=root).stdout.strip()
        status_before = git(["status", "--porcelain=v1"], cwd=root).stdout
        meta = Path(tmp) / "meta"
        meta.mkdir()
        snap_c0, snap = snapshot_git(root, meta, "test")
        check("HEAD unchanged", git(["rev-parse", "HEAD"], cwd=root).stdout.strip() == head_before)
        check("C0 recorded", snap_c0 == head_before)
        check("index blob unchanged", git(["show", ":feature_0"], cwd=root).stdout == index_before)
        check("status shape preserved", git(["status", "--porcelain=v1"], cwd=root).stdout == status_before)
        ls = git(["ls-tree", "-r", "--name-only", snap], cwd=root).stdout
        check("untracked in SNAP", "feature_1" in ls)
        check("scratch source in SNAP", "scratch/source.txt" in ls)
        wt1 = Path(tmp) / "wt1"
        add_worktree(root, wt1, snap)
        (wt1 / "helper.py").write_text("x = 1\n")
        (wt1 / "feature_0").write_text("line1\nstaged\nline3\nunstaged\nfrom helper import x\n")
        sess = {
            "id": "cap1",
            "wt1": str(wt1),
            "snap": snap,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(root),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("cap1").mkdir(parents=True)
        save_session(sess)
        tree, apply_patch, _, paths = capture_final_tree(sess)
        check("new file in inventory", "helper.py" in paths)
        check("new file in patch bytes", b"helper.py" in apply_patch.read_bytes())
        applied = apply_to_wt0(root, apply_patch, git_mode=True)
        check("apply succeeded", applied.returncode == 0)
        check("index still staged blob", git(["show", ":feature_0"], cwd=root).stdout == index_before)
        check("HEAD still", git(["rev-parse", "HEAD"], cwd=root).stdout.strip() == head_before)
        check("helper applied", (root / "helper.py").is_file())
        git(["worktree", "remove", "--force", str(wt1)], cwd=root, check=False)

        wt_rename = Path(tmp) / "wt-rename"
        add_worktree(root, wt_rename, snap)
        (wt_rename / "old.txt").unlink()
        (wt_rename / "new.txt").write_text("rename-me\n")
        sess_r = {
            "id": "cap2",
            "wt1": str(wt_rename),
            "snap": snap,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(root),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("cap2").mkdir(parents=True)
        save_session(sess_r)
        _tree, patch_r, _, names_r = capture_final_tree(sess_r)
        check("rename delete in inventory", "old.txt" in names_r)
        check("rename add in inventory", "new.txt" in names_r)
        git(["worktree", "remove", "--force", str(wt_rename)], cwd=root, check=False)

        wt_space = Path(tmp) / "wt-space"
        add_worktree(root, wt_space, snap)
        (wt_space / "space name.txt").write_text("spaced-edit\n")
        sess_s = {
            "id": "cap3",
            "wt1": str(wt_space),
            "snap": snap,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(root),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("cap3").mkdir(parents=True)
        save_session(sess_s)
        _tree, _patch_s, _, names_s = capture_final_tree(sess_s)
        check("space name in inventory", "space name.txt" in names_s)
        git(["worktree", "remove", "--force", str(wt_space)], cwd=root, check=False)

        init_py = root / "__init__.py"
        init_py.write_text("orig\n")
        base = baseline_for_paths(root, ["__init__.py"])
        init_py.write_text("partial write\n")
        check("init py mutation detected", baseline_changed(root, base))

        ignored = Path(tmp) / "ignored-scratch"
        ignored.mkdir()
        git(["init", "-b", "main"], cwd=ignored)
        (ignored / ".gitignore").write_text("scratch/\n")
        (ignored / "ok.txt").write_text("ok\n")
        git(["add", ".gitignore", "ok.txt"], cwd=ignored)
        git([*GIT_IDENT, "commit", "-m", "base"], cwd=ignored)
        meta_i = Path(tmp) / "meta-i"
        meta_i.mkdir()
        _c0, snap_i = snapshot_git(ignored, meta_i, "ign")
        ls_i = git(["ls-tree", "-r", "--name-only", snap_i], cwd=ignored).stdout
        check("ignored scratch snapshot works", "ok.txt" in ls_i)

        plain = Path(tmp) / "plain"
        plain.mkdir()
        (plain / "readme.md").write_text("hello\n")
        (plain / ".gitignore").write_text("*.txt\n!keep.txt\nskip-me\n")
        (plain / "skip-me").write_text("secret\n")
        (plain / "keep.txt").write_text("kept\n")
        (plain / "drop.txt").write_text("ignored-txt\n")
        copy_wt = Path(tmp) / "copy-wt"
        copy_meta = Path(tmp) / "copy-meta"
        copy_meta.mkdir()
        copy_snap = snapshot_copy(plain, copy_wt, copy_meta)
        check("copy has source file", (copy_wt / "readme.md").read_text() == "hello\n")
        check("copy honors gitignore", not (copy_wt / "skip-me").exists())
        check("copy drops ignored txt", not (copy_wt / "drop.txt").exists())
        check("copy honors gitignore negation", (copy_wt / "keep.txt").read_text() == "kept\n")
        check("no git init in user tree", not (plain / ".git").exists())
        check("git init only in copy", (copy_wt / ".git").exists())
        check("copy SNAP recorded", bool(copy_snap))

        parent = Path(tmp) / "parent"
        parent.mkdir()
        git(["init", "-b", "main"], cwd=parent)
        pkg = parent / "package"
        pkg.mkdir()
        (pkg / "a.txt").write_text("old\n")
        git(["add", "package/a.txt"], cwd=parent)
        git([*GIT_IDENT, "commit", "-m", "pkg"], cwd=parent)
        copy_pkg = Path(tmp) / "copy-pkg"
        meta_pkg = Path(tmp) / "meta-pkg"
        meta_pkg.mkdir()
        snap_pkg = snapshot_copy(pkg, copy_pkg, meta_pkg)
        (copy_pkg / "a.txt").write_text("new\n")
        sess_p = {
            "id": "cap4",
            "wt1": str(copy_pkg),
            "snap": snap_pkg,
            "git_mode": False,
            "phase": "running",
            "apply_target": str(pkg),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("cap4").mkdir(parents=True)
        save_session(sess_p)
        _tree, patch_p, _, _names = capture_final_tree(sess_p)
        applied_p = apply_to_wt0(pkg, patch_p, git_mode=False)
        check("copy apply under ancestor", apply_ok(applied_p))
        check("copy apply mutated package file", (pkg / "a.txt").read_text() == "new\n")
        rev_p = apply_to_wt0(pkg, patch_p, git_mode=False, check_only=True, reverse=True)
        check("copy apply reverse-check", apply_ok(rev_p))

        crlf_root = Path(tmp) / "crlf"
        crlf_root.mkdir()
        git(["init", "-b", "main"], cwd=crlf_root)
        (crlf_root / "dos.txt").write_bytes(b"OLD\r\n")
        git(["add", "dos.txt"], cwd=crlf_root)
        git([*GIT_IDENT, "commit", "-m", "dos"], cwd=crlf_root)
        meta_c = Path(tmp) / "meta-c"
        meta_c.mkdir()
        _c0, snap_c = snapshot_git(crlf_root, meta_c, "crlf")
        wt_c = Path(tmp) / "wt-c"
        add_worktree(crlf_root, wt_c, snap_c)
        (wt_c / "dos.txt").write_bytes(b"NEW\r\n")
        sess_c = {
            "id": "cap5",
            "wt1": str(wt_c),
            "snap": snap_c,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(crlf_root),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("cap5").mkdir(parents=True)
        save_session(sess_c)
        _tree, patch_c, _, _ = capture_final_tree(sess_c)
        check("crlf bytes in apply patch", b"\r\n" in patch_c.read_bytes())
        git(["worktree", "remove", "--force", str(wt_c)], cwd=crlf_root, check=False)

        sid = "dup"
        session_dir(sid).mkdir(parents=True)
        save_json(session_path(sid), {"id": sid, "phase": "reviewable"})
        try:
            refuse_existing_session(sid)
            check("session collision helper", False)
        except SystemExit:
            check("session collision helper", True)

        git(["config", "filter.review.driver.clean", "cat"], cwd=root)
        check("dotted filter name", "review.driver" in defined_filters(root))

        nested_src = Path(tmp) / "nested-src"
        nested_src.mkdir()
        (nested_src / "vendor").mkdir()
        (nested_src / "vendor" / ".git").mkdir()
        (nested_src / "vendor" / "n.txt").write_text("n\n")
        nested_wt = Path(tmp) / "nested-wt"
        nested_meta = Path(tmp) / "nested-meta"
        nested_meta.mkdir()
        try:
            snapshot_copy(nested_src, nested_wt, nested_meta)
            check("copy refuses nested git", False)
        except SystemExit:
            check("copy refuses nested git", True)

        git(["config", "diff.upper.textconv", "awk '{print toupper($0)}'"], cwd=crlf_root)
        (crlf_root / ".gitattributes").write_text("*.txt diff=upper\n")
        env_t = os.environ.copy()
        idx_t = Path(tmp) / "textconv-index"
        env_t["GIT_INDEX_FILE"] = str(idx_t)
        git(["read-tree", snap_c], cwd=crlf_root, env=env_t)
        (crlf_root / "plain.txt").write_text("new\n")
        git(["add", "plain.txt"], cwd=crlf_root, env=env_t)
        tree_t = git(["write-tree"], cwd=crlf_root, env=env_t).stdout.strip()
        dest_t = Path(tmp) / "textconv.patch"
        write_apply_patch(crlf_root, snap_c, tree_t, dest_t, env_t)
        patch_t = dest_t.read_bytes()
        check("no textconv in apply patch", b"+NEW" not in patch_t and b"+new" in patch_t)

        parent_left = Path(tmp) / "parent-left"
        parent_left.mkdir()
        git(["init", "-b", "main"], cwd=parent_left)
        (parent_left / "package").mkdir()
        (parent_left / "package" / "a.txt").write_text("x\n")
        git(["add", "package/a.txt"], cwd=parent_left)
        git([*GIT_IDENT, "commit", "-m", "p"], cwd=parent_left)
        leftover = parent_left / "scratch" / "codex" / "old" / "wt"
        leftover.mkdir(parents=True)
        msg = unexplained_leftover_blocks(parent_left / "package")
        check("orphan parent leftover blocks child", msg is not None)

        copy_parent = Path(tmp) / "copy-parent"
        copy_parent.mkdir()
        (copy_parent / "package").mkdir()
        (copy_parent / "package" / "a.txt").write_text("x\n")
        leftover_c = copy_parent / "scratch" / "codex" / "old" / "wt"
        leftover_c.mkdir(parents=True)
        msg_c = unexplained_leftover_blocks(copy_parent / "package")
        check("orphan copy-mode parent leftover blocks child", msg_c is not None and "copy-parent" in msg_c)

        sid_launch = "launch1"
        session_dir(sid_launch).mkdir(parents=True)
        save_json(
            session_path(sid_launch),
            {
                "id": sid_launch,
                "phase": "running",
                "pid": os.getpid(),
                "codex_launch": "pending",
            },
        )
        cmd_recover(argparse.Namespace(session=sid_launch))
        check("recover during launch stays running", load_session(sid_launch)["phase"] == "running")

        sid_dead = "launch2"
        session_dir(sid_dead).mkdir(parents=True)
        save_json(
            session_path(sid_dead),
            {"id": sid_dead, "phase": "running", "pid": 999_999_999},
        )
        cmd_recover(argparse.Namespace(session=sid_dead))
        check(
            "recover dead runner no worker interrupts",
            load_session(sid_dead)["phase"] == "interrupted",
        )

        f2d = Path(tmp) / "f2d"
        f2d.mkdir()
        git(["init", "-b", "main"], cwd=f2d)
        (f2d / "package").write_text("was-file\n")
        git(["add", "package"], cwd=f2d)
        git([*GIT_IDENT, "commit", "-m", "file"], cwd=f2d)
        meta_f = Path(tmp) / "meta-f2d"
        meta_f.mkdir()
        _c0, snap_f = snapshot_git(f2d, meta_f, "f2d")
        wt_f = Path(tmp) / "wt-f2d"
        add_worktree(f2d, wt_f, snap_f)
        (wt_f / "package").unlink()
        (wt_f / "package").mkdir()
        (wt_f / "package" / "__init__.py").write_text("pkg\n")
        sess_f = {
            "id": "f2d",
            "wt1": str(wt_f),
            "snap": snap_f,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(f2d),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("f2d").mkdir(parents=True)
        save_session(sess_f)
        _tree, patch_f, _, names_f = capture_final_tree(sess_f)
        try:
            expected_f = expected_after_apply(f2d, patch_f, names_f, git_mode=True)
            check(
                "file to dir expected",
                str(expected_f.get("package/__init__.py", "")).startswith("FILE:"),
            )
        except SystemExit:
            check("file to dir expected", False)
        git(["worktree", "remove", "--force", str(wt_f)], cwd=f2d, check=False)

        crlf_attr = Path(tmp) / "crlf-attr"
        crlf_attr.mkdir()
        git(["init", "-b", "main"], cwd=crlf_attr)
        (crlf_attr / ".gitattributes").write_text("*.txt text eol=crlf\n")
        (crlf_attr / "dos.txt").write_bytes(b"OLD\r\n")
        git(["add", ".gitattributes", "dos.txt"], cwd=crlf_attr)
        git([*GIT_IDENT, "commit", "-m", "crlf"], cwd=crlf_attr)
        meta_ca = Path(tmp) / "meta-ca"
        meta_ca.mkdir()
        _c0, snap_ca = snapshot_git(crlf_attr, meta_ca, "ca")
        wt_ca = Path(tmp) / "wt-ca"
        add_worktree(crlf_attr, wt_ca, snap_ca)
        (wt_ca / "dos.txt").write_bytes(b"NEW\r\n")
        sess_ca = {
            "id": "ca",
            "wt1": str(wt_ca),
            "snap": snap_ca,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(crlf_attr),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("ca").mkdir(parents=True)
        save_session(sess_ca)
        _tree, patch_ca, _, names_ca = capture_final_tree(sess_ca)
        try:
            expected_ca = expected_after_apply(crlf_attr, patch_ca, names_ca, git_mode=True)
            check("crlf attributes expected", "dos.txt" in expected_ca)
        except SystemExit:
            check("crlf attributes expected", False)
        git(["worktree", "remove", "--force", str(wt_ca)], cwd=crlf_attr, check=False)

        immune = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        stopped_i = terminate_spawned(immune)
        check("term-immune spawned group killed", stopped_i and immune.poll() is not None)

        link_root = Path(tmp) / "link-root"
        link_root.mkdir()
        git(["init", "-b", "main"], cwd=link_root)
        (link_root / "dos.txt").write_bytes(b"OLD\r\n")
        git(["add", "dos.txt"], cwd=link_root)
        git([*GIT_IDENT, "commit", "-m", "base"], cwd=link_root)
        linked = Path(tmp) / "linked"
        git(["worktree", "add", str(linked), "HEAD"], cwd=link_root)
        info_raw = git(["rev-parse", "--git-path", "info/attributes"], cwd=linked).stdout.strip()
        info_p = Path(info_raw) if Path(info_raw).is_absolute() else linked / info_raw
        info_p.parent.mkdir(parents=True, exist_ok=True)
        info_p.write_text("*.txt text eol=crlf\n")
        meta_l = Path(tmp) / "meta-l"
        meta_l.mkdir()
        _c0, snap_l = snapshot_git(linked, meta_l, "link")
        wt_l = Path(tmp) / "wt-l"
        add_worktree(linked, wt_l, snap_l)
        (wt_l / "dos.txt").write_bytes(b"NEW\r\n")
        sess_l = {
            "id": "link",
            "wt1": str(wt_l),
            "snap": snap_l,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(linked),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("link").mkdir(parents=True)
        save_session(sess_l)
        _tree, patch_l, _, names_l = capture_final_tree(sess_l)
        try:
            expected_l = expected_after_apply(linked, patch_l, names_l, git_mode=True)
            check("linked worktree info/attributes expected", "dos.txt" in expected_l)
        except SystemExit:
            check("linked worktree info/attributes expected", False)
        git(["worktree", "remove", "--force", str(wt_l)], cwd=linked, check=False)
        git(["worktree", "remove", "--force", str(linked)], cwd=link_root, check=False)

        attr_root = Path(tmp) / "attr-root"
        attr_root.mkdir()
        git(["init", "-b", "main"], cwd=attr_root)
        attr_file = Path(tmp) / "extra.attributes"
        attr_file.write_text("*.txt text eol=crlf\n")
        git(["config", "core.attributesFile", str(attr_file)], cwd=attr_root)
        (attr_root / "dos.txt").write_bytes(b"OLD\r\n")
        git(["add", "dos.txt"], cwd=attr_root)
        git([*GIT_IDENT, "commit", "-m", "base"], cwd=attr_root)
        meta_af = Path(tmp) / "meta-af"
        meta_af.mkdir()
        _c0, snap_af = snapshot_git(attr_root, meta_af, "af")
        wt_af = Path(tmp) / "wt-af"
        add_worktree(attr_root, wt_af, snap_af)
        (wt_af / "dos.txt").write_bytes(b"NEW\r\n")
        sess_af = {
            "id": "af",
            "wt1": str(wt_af),
            "snap": snap_af,
            "git_mode": True,
            "phase": "running",
            "apply_target": str(attr_root),
            "effort": "high",
            "auth_mode": "chatgpt",
            "network_inherited": False,
        }
        session_dir("af").mkdir(parents=True)
        save_session(sess_af)
        _tree, patch_af, _, names_af = capture_final_tree(sess_af)
        try:
            expected_af = expected_after_apply(attr_root, patch_af, names_af, git_mode=True)
            check("core.attributesFile expected", "dos.txt" in expected_af)
        except SystemExit:
            check("core.attributesFile expected", False)
        git(["worktree", "remove", "--force", str(wt_af)], cwd=attr_root, check=False)

        saved_home = os.environ["CODEX_SKILL_HOME"]
        os.environ["CODEX_SKILL_HOME"] = str(Path.home() / ".codex-skill-self-test-home")
        try:
            refuse_bad_writable_roots(
                Path.home(),
                Path("/tmp/codex-skill-meta-probe"),
                Path.home() / "scratch" / "codex" / "x" / "wt",
            )
            check("metadata under tmp refused", False)
        except SystemExit:
            check("metadata under tmp refused", True)
        os.environ["CODEX_SKILL_HOME"] = saved_home

        os.environ.pop("CODEX_SKILL_HOME", None)

    if failures:
        die(f"{failures} self-test failures")
    print("self-test passed")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codex-skill-run")
    sub = p.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--wt0", required=True)
    start.add_argument("--task-file", required=True)
    start.add_argument("--effort", default="high")
    start.add_argument("--session")
    start.set_defaults(func=cmd_start)
    st = sub.add_parser("status")
    st.add_argument("--session")
    st.add_argument("--wt0")
    st.set_defaults(func=cmd_status)
    pk = sub.add_parser("packet")
    pk.add_argument("--session", required=True)
    pk.set_defaults(func=cmd_packet)
    az = sub.add_parser("authorize")
    az.add_argument("--session", required=True)
    az.add_argument("--patch")
    az.set_defaults(func=cmd_authorize)
    rj = sub.add_parser("reject")
    rj.add_argument("--session", required=True)
    rj.set_defaults(func=cmd_reject)
    ab = sub.add_parser("abandon")
    ab.add_argument("--session", required=True)
    ab.set_defaults(func=cmd_abandon)
    rc = sub.add_parser("recover")
    rc.add_argument("--session", required=True)
    rc.set_defaults(func=cmd_recover)
    stt = sub.add_parser("self-test")
    stt.set_defaults(func=cmd_self_test)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
