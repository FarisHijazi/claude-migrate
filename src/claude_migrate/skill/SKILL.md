---
name: migrate
argument-hint: [<new_path> | [user@]host:<new_path>]
description: Migrate Claude Code — move a project's conversation history to a new path on the same machine or to another machine over ssh, and optionally the whole ~/.claude setup (settings, skills, commands, agents, hooks, memory, credentials), so Claude runs signed-in with full history at the destination. Single /migrate entry point for same-machine and cross-machine moves. Use whenever the user moves a project directory, or talks about moving their work, setup, config, history or "everything" to another machine, a server, a VM, a new laptop, a dev box or a remote host; when they say "continue there", "not sign in again", "keep my history", or "sync my .claude"; and when a migrated Claude Code is missing history, asks to log in again, hits .incoming conflicts, or its hooks broke after a move. NOT for database schema migrations, data/ETL migrations, or porting code between frameworks.
---

# `/migrate` — move Claude Code history and setup

**Destination** = `$ARGUMENTS`. A `:` before the path (`host:/path`) means another
machine; otherwise same machine. No argument: ask.

**The problem this exists to solve.** `~/.claude/projects/` names each folder after the
project's absolute path with `/` → `-` (`/Users/me/app` → `-Users-me-app`). Move the
project and the history is orphaned: every byte still there, `--continue` sees nothing.
So this is never a file copy. Two things must change: the folder **name**, and the old
path written **inside** the transcripts.

| Case | Do |
|---|---|
| New path, same machine | Step 1, local |
| History onto another machine | Step 1, remote |
| Whole setup ("everything", "not sign in again") | Step 1 + `references/platform-notes.md` |

Default to history-only. Copying credentials is a bigger action than most asks imply;
offer it, don't assume it.

---

## Step 0 — clear `.incoming` on BOTH machines. Always.

```bash
find ~/.claude/projects -name "*.incoming*"
ssh <host> 'find ~/.claude/projects -name "*.incoming*"'
```

Must be empty before you continue. See [Conflicts](#conflicts) to resolve any hits.

> **Consequence if skipped:** `claude-migrate` writes a `.incoming` per conflict and never
> cleans up. Each run appends *another* suffix — `X.jsonl.incoming.incoming.incoming…` —
> growing one level per run. Past ~23 the name breaks the 255-byte limit and the import
> dies with `OSError: [Errno 36] File name too long`, aborting that project **silently**
> while other projects succeed. Stale files also ride along in the export archive, so a
> dirty source re-infects the destination.

## Step 1 — move the history

Always the git URL. **Never `uvx claude-migrate`** (the PyPI build lags and has no
`export`/`import`).

**Same machine:**

```bash
uvx git+https://github.com/FarisHijazi/claude-migrate migrate "$(pwd)" "<new_path>"
```

`--dry-run` first. `--merge` if the destination already has history. (`cp` and `mv` still
work, but are deprecated aliases of `migrate` and `migrate --delete-history`.)

To move the project **folder** as well as its history, add `--folder`, plus any of:

| Flag | Effect |
|---|---|
| `--delete-history` | Move the history instead of copying it |
| `--delete-dir` | Also delete the source folder (requires `--folder`) |
| `--replace-userpath[=FROM:TO]` | Rewrite `/Users/<user>` and `/home/<user>` inside chat content; the bare flag auto-detects, `=FROM:TO` overrides |
| `--replace-references` | Rewrite absolute and relative path references inside chat content |

**Another machine:**

```bash
# 1. archive
uvx git+https://github.com/FarisHijazi/claude-migrate export "$(pwd)" -o /tmp

# 2. copy (exact filename the export printed)
scp /tmp/<archive>.tar.gz <host>:/tmp/

# 3. ensure uvx there
ssh <host> 'export PATH="$HOME/.local/bin:$PATH"
  command -v uvx >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'

# 4. import — re-keys the folder AND rewrites the old path inside the transcripts
ssh <host> 'export PATH="$HOME/.local/bin:$PATH"
  uvx git+https://github.com/FarisHijazi/claude-migrate import \
    /tmp/<archive>.tar.gz "<remote_path>"'
```

> **`export PATH` on every remote line, not just step 3.** `uvx` and `claude` live in
> `~/.local/bin`, and a non-interactive ssh does not read `.bashrc`. Without it,
> `command -v uvx` reports MISSING right after a successful install and reinstalls forever.

> **Never `scp -r` the `projects/<dir>` yourself.** It lands under a name the target never
> looks up, and skips the in-transcript rewriting. Both look like success at the time.

### Build `<remote_path>` from the target's real `$HOME`

```bash
ssh <host> 'echo $HOME'
```

> **Consequence if wrong:** no error, just a folder nobody reads. Seen live: a doubled
> segment (`…/thmanyah-d-thmanyah-d-career-coach`) left 430 MB and 80 sessions permanently
> invisible while `--continue` showed only the few sessions started natively on that box.

## Step 2 — verify the mapping landed

File counts prove nothing. Check the `cwd` values:

```bash
ssh <host> 'cat ~/.claude/projects/<slug>/*.jsonl' | python3 -c "
import sys,json,collections
c=collections.Counter()
for l in sys.stdin:
    try:
        d=json.loads(l)
        if isinstance(d,dict) and 'cwd' in d: c[d['cwd']]+=1
    except Exception: pass
for k,v in c.most_common(10): print(f'{v:>7}  {k}')"
```

Every `cwd` must be the destination path (nested workspace paths *under* it are fine).

Then confirm only one folder exists for this project:

```bash
ssh <host> 'ls -d ~/.claude/projects/*<project-name>*'
```

**Found a second, mis-keyed one? Diff before deleting it.**

```bash
comm -23 <(ls <orphan>/*.jsonl | xargs -n1 basename | sort) \
         <(ls <good>/*.jsonl   | xargs -n1 basename | sort)   # MUST be empty
```

> **Consequence if skipped:** a mis-keyed folder usually also holds sessions that ran *on
> that box* while it was mis-keyed, which exist nowhere else. A real orphan looked like a
> pure duplicate of 80 sessions but held **3 unique ones**, including a full `/brief` run.

Rescue anything unique, then re-diff to zero. Prefer `mv` to `/tmp` over `rm` — same
filesystem, free, recoverable until reboot.

```bash
sed "s|<bad_path>|<good_path>|g" "<orphan>/$s.jsonl" > "<good>/$s.jsonl"
```

## Step 3 — prove it works

```bash
ssh <host> 'export PATH="$HOME/.local/bin:$PATH"; cd <remote_path>
  claude --continue -p "In one line, what were we last working on?"'
```

A real answer means it is wired up. `OAuth session expired and could not be refreshed`
is the *credentials* layer, not history — check for a missing `refreshToken`
(`platform-notes.md`), verify history via Step 2 instead, and report the auth blocker
separately rather than calling the migration unverified.

---

## Conflicts

A merge onto existing history reports `conflict: N` and writes `.incoming` files.

**Check for collisions first — usually there are none and there is nothing to resolve:**

```bash
comm -12 <(ls <src>/*.jsonl | xargs -n1 basename | sort) \
         <(ls <dst>/*.jsonl | xargs -n1 basename | sort)
```

Empty = purely additive merge. Done.

⚠️ **Never auto-promote `.incoming`.** Older guidance called it "the freshly rewritten one,
so usually the keeper". That is **wrong in both directions**, measured on real data:

- `.incoming` copies carried **3–20× more unrewritten source paths** than their originals.
  A *larger* `.incoming` is the tell, because the source path is the longer string.
- One session had run on both machines: destination 547 lines ending 13:16, `.incoming`
  388 lines ending 08:44. Taking `.incoming` would have destroyed 4.5 h of conversation.

Compare on each side — `wc -l`, last `"timestamp"`, count of foreign paths — and promote
only a provable superset:

```bash
head -n $(wc -l < "$orig") "$incoming" | cmp - "$orig"   # silent = safe to promote
```

Import rewrites **only its own project prefix**, so cross-project `cwd` references survive.
Sweep leftovers yourself, but remap **only paths that exist on both hosts** — rewriting a
source-only path fabricates a directory that does not exist.

---

## The two scripts

### 1. `claude-migrate` (the `uvx git+…` tool) — one-off moves

Used in Step 1. Three things a copy cannot do: renames the folder to the destination's
path-slug, rewrites the old absolute path inside every transcript, and merges rather than
overwrites (so re-running is safe and reports `identical`). Ships `cp` / `mv` / `export` /
`import` — the last two only from the git URL, never PyPI.

### 2. `claude-migrate sync` — keeping two machines in sync afterwards

A migration is a one-off; work then happens on both machines. This runs two-way passes.
Same tool, so there is nothing extra to install.

```bash
uvx git+https://github.com/FarisHijazi/claude-migrate sync              # one pass
uvx git+https://github.com/FarisHijazi/claude-migrate sync -n -v        # dry run
uvx git+https://github.com/FarisHijazi/claude-migrate sync --interval 300
```

**How it decides what to move:**

| Rule | Effect |
|---|---|
| Syncs only projects whose directory exists on **both** machines | The entire scoping model. No allowlist, and no work project leaks onto a box that lacks it. |
| Reads each project's real path from its **transcripts**, not the folder name | The name is lossy: `foo-bar` and `foo/bar` are identical after mangling. |
| Skips transcripts touched within `settle_seconds` | Syncing a live session reliably manufactures a conflict. |
| Keeps **no state file** | `import` merges, so a repeat run is a no-op. Nothing to go stale. |
| Drives both directions from whichever side can connect | In a laptop/server pair usually only one side is reachable. |

Config `~/.claude/history-sync.json`:

```json
{"peers": [{"name": "dev", "ssh": "user@host",
            "identity": "~/.ssh/id_ed25519",
            "path_map": {"/Users/me": "/home/user"}}],
 "settle_seconds": 60}
```

Set `identity` explicitly — a timer-launched run has no ssh agent. On macOS the LaunchAgent
also needs `PATH` in `EnvironmentVariables`, or `uvx` is not found.

**Its one failure mode: unresolved `.incoming` freezes a project.**

The sync refuses any project holding them:
`! <project>: unresolved .incoming conflict - skipping until you resolve it`. That
project's history then stops moving on **both** machines while everything else keeps
syncing — which is exactly why it goes unnoticed. Meanwhile the conflict files are
themselves synced and re-conflict, so the suffix compounds every run (see Step 0).

Diagnose whenever "the sync seems fine but a project is stale":

```bash
find ~/.claude/projects -name "*.incoming*" | wc -l
tail -30 ~/.claude/history-sync.log | grep '!'
```

Resolution is a judgement call about which transcript to keep, so it needs the user.
Follow [Conflicts](#conflicts); never auto-resolve.

### Running `sync` on a schedule

Both scripts live in the `claude-migrate` repo, so a scheduler entry only ever needs the
`uvx` line above — nothing local to relocate. If you do change how a unit invokes it,
remember a scheduler pointing at a stale path fails **silently**:

```bash
grep -rl "claude-migrate\|claude-history-sync" ~/Library/LaunchAgents ~/.config/systemd 2>/dev/null
```

Update every hit, reload the unit, kick a run, read the log. A LaunchAgent needs `PATH` in
`EnvironmentVariables` or `uvx` is not found. (`PlistBuddy` rewrites plists canonically and
drops XML comments — keep rationale here instead.)

---

## Full setup migration

Only when the user wants more than history. `~/.claude` is four layers that fail four
ways; **`references/platform-notes.md` has the exclude table, credential locations, rsync
flavors and the verification ladder.** Read it before the first copy, not after.

| Layer | Fails as |
|---|---|
| Config & extensions (`settings.json`, `skills/`, `commands/`, `hooks/`, `memory/`) | hooks error every turn |
| Credentials (**macOS Keychain**, elsewhere `.credentials.json`) | asks you to sign in again |
| Project history (`projects/`) | `--continue` finds nothing → Step 1 above |
| Native deps (`plugins/cache`, `node_modules`, `node`, `python3`) | plugins/statusline break |

Three traps worth knowing before you open that file:

- **Anchor excludes with a leading `/`.** `--exclude 'tmp/'` matches at every depth and
  silently drops `jobs/*/tmp/`.
- **On macOS the Keychain is the live credential**; the on-disk file is often days stale
  and copying it fails at first use. Size-compare, pipe over **stdin** (never argv).
- **If the target is a live box, inventory what runs on it first.** A mirror silently rolls
  back watcher cursors, work claims and any config gating outbound sending — that is how
  you re-send real messages to real people. Ask which side wins.

Never delete the source `~/.claude` while a session is running there. Confirm copy, not
move.

## Reporting

Lead with what was **verified**, not what was copied: "resume works" beats "2.0 GB
transferred". Then the credential expiry (and whether a `refreshToken` exists at all),
anything you edited on the target and where the backup is, and remaining manual steps with
exact commands. Call out near-misses explicitly — a stale credential, an over-broad
exclude, a mis-keyed slug, a live state file you chose not to overwrite. Those are the ones
that resurface hours later as "Claude is broken on the new box".
