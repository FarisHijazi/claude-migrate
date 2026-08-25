"""Two-way, idempotent sync of Claude Code conversation history between machines.

Syncs exactly the projects whose directory exists on BOTH machines. That rule is
the entire scoping model, so there are no allowlists to maintain and no work
project leaks onto a box that does not have it checked out.

Idempotent by construction: `claude-migrate import` merges rather than
overwrites, so a second run reports `identical` and changes nothing. There is no
state file to go stale, so re-running is always safe.

Both directions run from whichever side can open the connection, because in a
laptop/server pair usually only one side is reachable.

    claude-migrate sync                 # one pass
    claude-migrate sync -n -v           # show what would move
    claude-migrate sync --interval 300  # loop

Config: ~/.claude/history-sync.json
    {"peers": [{"ssh": "user@host",
                "identity": "~/.ssh/id_ed25519",
                "path_map": {"/Users/me": "/home/user"}}]}
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CONFIG = CLAUDE / "history-sync.json"
LOCK = CLAUDE / ".history-sync.lock"
MIGRATE = "uvx git+https://github.com/FarisHijazi/claude-migrate"
# Locally we ARE the tool - no need to re-resolve ourselves over the network.
LOCAL = [sys.executable, "-m", "claude_migrate.cli"]
# uv lives in ~/.local/bin and a non-interactive ssh does not read .bashrc.
RPATH = 'export PATH="$HOME/.local/bin:$PATH"; '

# Each project's real path comes from its own transcripts. The directory name
# cannot be trusted: it is the path with "/" replaced by "-", so "foo-bar" is
# indistinguishable from "foo/bar" (and "demaenergy.d" would read as
# "demaenergy/d").
LIST_REMOTE = r"""python3 -c '
import json,os,glob
out={}
for d in sorted(glob.glob(os.path.expanduser("~/.claude/projects")+"/*/")):
    for f in sorted(glob.glob(d+"*.jsonl")):
        try:
            for line in open(f,encoding="utf-8",errors="replace"):
                try: cwd=json.loads(line).get("cwd")
                except Exception: continue
                if cwd:
                    out[cwd]=max(out.get(cwd,0),os.path.getmtime(f)); break
        except OSError: pass
print(json.dumps(out))'"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def local_projects():
    """{project_path: newest transcript mtime}"""
    out = {}
    root = CLAUDE / "projects"
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl")):
            try:
                for line in f.open(encoding="utf-8", errors="replace"):
                    try:
                        cwd = json.loads(line).get("cwd")
                    except json.JSONDecodeError:
                        continue
                    if cwd:
                        out[cwd] = max(out.get(cwd, 0), f.stat().st_mtime)
                        break
            except OSError:
                pass
    return out


class Peer:
    def __init__(self, spec):
        self.target = spec["ssh"]
        self.name = spec.get("name") or self.target
        ident = spec.get("identity")
        self.ident = str(Path(ident).expanduser()) if ident else None
        self.map = spec.get("path_map", {})

    def _flags(self, tool):
        c = [tool] + (["-i", self.ident] if self.ident else [])
        return c + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]

    def ssh(self, script):
        return sh(self._flags("ssh") + [self.target, script])

    def scp(self, src, dst):
        return sh(self._flags("scp") + ["-q", src, dst])

    @staticmethod
    def _swap(path, mapping):
        for src, dst in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            src, dst = src.rstrip("/"), dst.rstrip("/")
            if path == src or path.startswith(src + "/"):
                return dst + path[len(src):]
        return path

    def to_remote(self, p):
        return self._swap(p, self.map)

    def to_local(self, p):
        return self._swap(p, {v: k for k, v in self.map.items()})

    def remote_projects(self):
        r = self.ssh(LIST_REMOTE)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            return {}


def slug_of(path):
    """The history directory name Claude derives from a project path."""
    return str(path).replace("/", "-")


def shared(peer, settle):
    """Projects present on both machines and not currently being written."""
    home = str(Path.home())
    now = time.time()
    mine, theirs = local_projects(), peer.remote_projects()

    cands = {c for c in set(mine) | {peer.to_local(p) for p in theirs}
             if c.rstrip("/") != home and Path(c).is_dir()}
    if not cands:
        return []

    pairs = [(c, peer.to_remote(c)) for c in sorted(cands)]
    q = " ".join(shlex.quote(rp) for _, rp in pairs)
    present = set(peer.ssh(f'for p in {q}; do [ -d "$p" ] && echo "$p"; done').stdout.split())

    out = []
    for lp, rp in pairs:
        if rp not in present:
            continue
        # An unresolved .incoming sits INSIDE the history directory, so the next
        # export ships it, it conflicts again, and becomes .incoming.incoming --
        # once per run, forever, until the filename exceeds the OS limit and
        # every import for that project dies. Refuse to sync until a human
        # resolves it; a blocked project is far cheaper than a runaway one.
        stuck = list((CLAUDE / "projects").glob(f"*/**/*.incoming"))
        if any(str(Path(lp).name) in str(s) or slug_of(lp) in str(s) for s in stuck):
            log(f"  ! {os.path.basename(lp)}: unresolved .incoming conflict - "
                f"skipping until you resolve it (see ~/.claude/projects)")
            continue
        # A transcript touched seconds ago is probably mid-write, and syncing a
        # live session is the one reliable way to manufacture a merge conflict.
        if now - max(mine.get(lp, 0), theirs.get(rp, 0)) < settle:
            continue
        out.append((lp, rp))
    return out


def move(direction, peer, src, dst, tmp, dry):
    """Export one side, import the other. direction is 'push' or 'pull'."""
    arrow = "->" if direction == "push" else "<-"
    if dry:
        log(f"  [dry-run] {os.path.basename(src)} {arrow} {peer.name}")
        return True

    if direction == "push":
        exp = sh(LOCAL + ["export", src, "-o", tmp])
    else:
        exp = peer.ssh(RPATH + f"{MIGRATE} export {shlex.quote(src)} -o /tmp")
    name = next((t for t in exp.stdout.replace(":", " ").split()
                 if t.endswith(".tar.gz")), None)
    if exp.returncode != 0 or not name:
        log(f"  ! {direction} {src}: export failed")
        return False
    name = os.path.basename(name)
    qname = shlex.quote(name)

    if direction == "push":
        moved = peer.scp(os.path.join(tmp, name), f"{peer.target}:/tmp/{name}").returncode == 0
    else:
        moved = peer.scp(f"{peer.target}:/tmp/{name}", tmp).returncode == 0
        peer.ssh(f"rm -f /tmp/{qname}")
    if not moved:
        log(f"  ! {direction} {src}: transfer failed")
        return False

    if direction == "push":
        imp = peer.ssh(RPATH + f"{MIGRATE} import /tmp/{qname} {shlex.quote(dst)}"
                       f"; rc=$?; rm -f /tmp/{qname}; exit $rc")
    else:
        imp = sh(LOCAL + ["import", os.path.join(tmp, name), dst])
    for leftover in Path(tmp).glob("*.tar.gz"):
        leftover.unlink(missing_ok=True)

    if imp.returncode != 0:
        log(f"  ! {direction} {src}: import failed: {(imp.stderr or imp.stdout).strip()[:140]}")
        return False

    counts = " ".join(l.strip() for l in imp.stdout.splitlines()
                      if l.strip().split(":")[0].strip() in
                      ("identical", "upgraded", "added", "conflict", "skipped"))
    if "conflict:" in imp.stdout and "conflict: 0" not in imp.stdout:
        counts += "   CONFLICT -> .incoming files left for you to resolve"
    log(f"  {arrow} {os.path.basename(src)}  {counts or 'ok'}")
    return True


def once(cfg, args):
    peers = [Peer(p) for p in cfg.get("peers", []) if p.get("ssh") not in (None, "user@host")]
    if not peers:
        print(f"No peers configured in {CONFIG}", file=sys.stderr)
        return 2
    failures = 0
    for peer in peers:
        pairs = shared(peer, cfg.get("settle_seconds", 60))
        if not pairs:
            if args.verbose:
                log(f"{peer.name}: nothing shared and settled")
            continue
        if not args.dry_run:
            peer.ssh(RPATH + "command -v uvx >/dev/null || "
                             "curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1")
        with tempfile.TemporaryDirectory(prefix="claude-hsync-") as tmp:
            for lp, rp in pairs:
                failures += not move("push", peer, lp, rp, tmp, args.dry_run)
                failures += not move("pull", peer, rp, lp, tmp, args.dry_run)
    return 1 if failures else 0


CONFIG_EXAMPLE = """{
  "peers": [{"name": "dev", "ssh": "user@host",
             "identity": "~/.ssh/id_ed25519",
             "path_map": {"/Users/me": "/home/user"}}],
  "settle_seconds": 60
}"""


def run(args):
    """`claude-migrate sync`. args carries .interval, .dry_run, .verbose."""
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        print(f"Missing or invalid {CONFIG}. Expected shape:\n{CONFIG_EXAMPLE}",
              file=sys.stderr)
        return 2

    CLAUDE.mkdir(parents=True, exist_ok=True)
    fh = LOCK.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another run is in flight

    if not args.interval:
        return once(cfg, args)
    while True:
        try:
            once(cfg, args)
        except Exception as exc:
            log(f"ERROR {exc!r}")
        time.sleep(args.interval)
