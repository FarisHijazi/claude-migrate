![`claude-migrate`](./docs/banner.png)

You can finally take your `claude code` history with you when you move/rename projects folders!  


Normally, your history is stored based on the project path, if you rename it you lose the ability to `--resume` or `--continue`.  
This tool copies the history from claude's internal files in `~/.claude/projects/...`.

![claude-migrate](./docs/demo-recording-600-fast.gif)


## Quick start (Claude Code plugin)

The repo is its own plugin marketplace. Inside Claude Code:

```
/plugin marketplace add FarisHijazi/claude-migrate
/plugin install claude-migrate@farishijazi
```

That installs the `/migrate` skill. Then, in the old location:

```
/migrate /new/path                  # same machine
/migrate user@host:/new/path        # another machine
```

Create the new path (`cp -r /old/path /new/path`) either before or after.

<details>
<summary>Without the plugin system</summary>

```bash
# install `uv` and `uvx` if you haven't already
# curl -LsSf https://astral.sh/uv/install.sh | sh
uvx git+https://github.com/FarisHijazi/claude-migrate install-skill
```

Installs the same skill to `~/.claude/skills/migrate/`. `install-slash-command` still
works as an alias. If you have an older `~/.claude/commands/migrate.md`, delete it — two
things answering to `/migrate` drift apart.

</details>

## CLI Usage

```bash
# Preview a copy of the history (default behavior, keeps source)
uvx git+https://github.com/FarisHijazi/claude-migrate migrate /old/path /new/path --dry-run

# Move history (copy + delete source history)
uvx claude-migrate migrate /old/path /new/path --delete-history

# Move the project folder AND its history in one shot
uvx claude-migrate migrate /old/path /new/path \
    --folder --delete-history --delete-dir

# Then continue at the new location
cd /new/path && claude --continue
```

### Quick tip

Inside Claude Code with the `!` prefix (no AI overhead):

```
! uvx claude-migrate migrate "$(pwd)" /new/path
```

## Commands

| Command | Description |
|---------|-------------|
| `migrate <old> <new>` | Migrate history (and optionally the folder) between paths |
| `rm <path>` | Remove history for a directory (add `--folder` to also delete the directory itself) |
| `export <project_path>` | Export one project's history to a portable `.tar.gz` archive |
| `export-all` | Export **all** project histories under `~/.claude/projects/` to one archive |
| `import <archive> [target]` | Unpack and smart-merge into the target path (rewrites paths) |
| `import-all <archive>` | Import an `export-all` archive into local `~/.claude/projects/` |
| `sync` | Two-way sync with peers in `~/.claude/history-sync.json` |
| `user-messages` | Extract just the human-typed messages from the current session |
| `install-skill` | Install the `/migrate` skill into `~/.claude/skills` |
| `cp <old> <new>` | Deprecated alias of `migrate` |
| `mv <old> <new>` | Deprecated alias of `migrate --delete-history` |
| `install-slash-command` | Install the `/migrate` slash command |

All commands support `--dry-run` / `-n`.

### `migrate` flags

| Flag | Effect |
|---|---|
| `--merge`, `-m` | Smart-merge into existing history at the destination |
| `--folder` | Also copy the actual project folder (errors if destination folder already exists) |
| `--delete-history` | Delete the source history after copy (move semantics for history) |
| `--delete-dir` | Delete the source project folder after copy (requires `--folder`) |
| `--replace-userpath [FROM:TO]` | Rewrite `/Users/<x>/` and `/home/<x>/` in chat content. Bare flag auto-detects; pass `--replace-userpath=alice:bob` to override |
| `--replace-references` | Walk every JSON/JSONL value in the history and rewrite absolute and relative path references inside chat content (see below) |

### `--replace-references` — how path references are rewritten

When you migrate a project, the chat history is full of path strings inside tool calls and prose. With `--replace-references`, every string value in every JSON/JSONL line is scanned:

- **Absolute paths under the old CWD** are rewritten under the new CWD. Boundary-protected so `/old/cwd` doesn't corrupt `/old/cwdbar`.
- **Encoded forms** (e.g. `-Users-alice-projects-foo`) are also rewritten.
- **Relative paths** (`./foo`, `../bar/baz`) are resolved against the old CWD:
  - If they resolve **inside** the old CWD, they're kept as relative — they still work from the new location.
  - If they **escape** the old CWD (e.g. `../other/file.py`), they're pinned to the **original absolute path**, because the external file didn't move.

Every unique `before -> after` replacement is printed before being applied. Use `--dry-run` to preview without writing.

### Cross-machine / cross-user

```bash
# On the source machine
uvx claude-migrate export-all -o ~/backups/

# Copy the .tar.gz to the target machine, then
uvx claude-migrate import-all ~/backups/claude-history-ALL-*.tar.gz \
    --replace-userpath --replace-references
```

`--replace-userpath` (bare flag) auto-detects the source user from the archive metadata and rewrites `/Users/<src>/` and `/home/<src>/` to your current user, including the encoded directory names. Add `--replace-references` for the deeper content rewrite.

Moving to another machine is `export` -> `scp` -> `import`. Only `import` re-keys the
folder **and** rewrites the old absolute path inside the transcripts, which is why
`scp -r` of `~/.claude/projects/<dir>` does not work: it lands under a name the target
never looks up and leaves the old paths embedded in the chat text.

## `sync` — keeping two machines in sync

Migration is a one-off; afterwards work happens on both machines.

```bash
claude-migrate sync                 # one pass
claude-migrate sync -n -v           # show what would move
claude-migrate sync --interval 300  # loop
```

`~/.claude/history-sync.json`:

```json
{"peers": [{"name": "dev", "ssh": "user@host",
            "identity": "~/.ssh/id_ed25519",
            "path_map": {"/Users/me": "/home/user"}}],
 "settle_seconds": 60}
```

| Rule | Why |
|---|---|
| Syncs only projects whose directory exists on **both** machines | The whole scoping model. No allowlist, and no project leaks onto a box that lacks it. |
| Reads each project's real path from its **transcripts**, not the folder name | The name is lossy: `foo-bar` and `foo/bar` mangle identically. |
| Skips transcripts touched within `settle_seconds` | Syncing a live session is the reliable way to manufacture a conflict. |
| Keeps **no state file** | `import` merges, so a repeat run is a no-op. Nothing to go stale. |
| Runs both directions from whichever side can connect | In a laptop/server pair usually only one side is reachable. |

Set `identity` explicitly — a timer-launched run has no ssh agent. A macOS LaunchAgent
also needs `PATH` in `EnvironmentVariables`, or `uvx` is not found.

## How it works

1. Claude Code encodes project paths by replacing `/` and `.` with `-` (e.g. `/home/user/project` -> `-home-user-project`)
2. History lives at `~/.claude/projects/<encoded-path>/` as JSONL files
3. `migrate` copies the history directory to the new encoded path
4. With `--replace-references` and/or `--replace-userpath`, it also rewrites string content inside each JSONL/JSON line
5. Appends a user message to the latest session noting the path change, so Claude knows files moved

## Gotchas

### Renaming a subdirectory has no history to migrate

History is bucketed by the **cwd Claude was launched from**, not by which files a session read or wrote. So if you renamed `~/foo/workspace/old-task/` -> `~/foo/workspace/20260519-task/`, there's no history bucket to move, because Claude was launched from `~/foo/`, never from inside the subdir. The transcripts referencing the old subdir name live inside `~/.claude/projects/-home-user-foo/` as immutable JSONL — they're history, not state, so `migrate` doesn't rewrite them.

When you actually need to migrate: rename the directory you `cd`'d into when running `claude`. That's the one with the `~/.claude/projects/<encoded-path>/` bucket.

If you want path references inside transcripts updated too (so future `--continue` sessions see the new names in scrollback), use `--replace-references` on a migrate command targeting the actual project root.

Per file, `import` reports `added`, `identical`, `upgraded`, `kept` or `conflict`.
Transcripts are append-only, so **when one side is a strict prefix of the other, the
longer one wins and the shorter is dropped** — it is the same session, continued.

That test runs on the transcript's **uuid spine** (its ordered `uuid` fields), not on raw
bytes, because `import` rewrites every embedded path: a transcript can share zero bytes
with its own earlier copy while being the same conversation. A byte compare therefore
reported `conflict` on every run for files that differed only by the rewrite.

`conflict` is reserved for the two cases where neither side provably contains the other —
the sessions **diverged**, or the spines are **equal length**. Those write a `.incoming`
file beside the original and need a human.

> ⚠️ **A `.incoming` is not automatically the one to keep.** Compare `wc -l`, the last
> `"timestamp"`, and the count of foreign paths on each side first. Observed in practice:
> `.incoming` copies that were *larger* only because they carried unrewritten paths, and
> one that was 159 lines **shorter** than the file it would have replaced.
>
> Resolve them, do not let them accumulate: an unresolved `.incoming` sits inside the
> history directory, so the next `export` ships it, it conflicts again, and becomes
> `.incoming.incoming` — one level per run. Past ~23 levels the filename exceeds the
> 255-byte limit and that project's import dies with `Errno 36`. `sync` refuses a project
> holding any, which freezes its history on **both** machines while everything else keeps
> syncing.

```bash
find ~/.claude/projects -name "*.incoming*"     # run on BOTH machines before migrating
```

## Reference

Based on: https://gist.github.com/gwpl/e0b78a711b4a6a2fc4b594c9b9fa2c4c
