# claude-migrate

CLI + Claude Code plugin for moving conversation history when a project moves — to a new
path, to another machine, or continuously between two machines.

## Structure

```
.claude-plugin/
  plugin.json          # plugin manifest
  marketplace.json     # the repo is its own marketplace ("farishijazi")
skills/
  migrate -> ../src/claude_migrate/skill      # SYMLINK, see below
src/claude_migrate/
  cli.py               # subcommands, encode_path, smart_merge_file, install
  sync.py              # `sync`: two-way peer sync. Imported LAZILY (needs fcntl,
                       #   which does not exist on Windows)
  skill/               # the /migrate skill — THE canonical copy
    SKILL.md
    references/platform-notes.md
tests/
  smart_merge_test.py  # uv run --with pytest pytest
```

**The skill exists once, at `src/claude_migrate/skill/`, and `skills/migrate` is a
symlink to it.** Both distribution channels need the same files in different places: the
plugin loader reads `skills/` from a git checkout (symlinks fine), while the wheel needs
real files under the package for `importlib.resources`. The symlink must point that
direction and not the other — **`uv_build` refuses to follow a symlinked directory** and
fails the build with `Is a directory (os error 21)`. Verified by building the wheel and
listing it; do that again if you ever move these files.

## Key details

- Claude Code encodes project paths by replacing `/` and `.` with `-`, so history is
  orphaned by any move. Renaming the directory is only half the job: `import` also
  rewrites the old absolute path *inside* the transcripts.
- **`smart_merge_file` compares the uuid spine, not bytes.** `import` rewrites every
  embedded path, so a transcript can share zero bytes with its own earlier copy while
  being the same conversation. Transcripts are append-only, so a strictly shorter spine
  that prefixes the other means the longer file already contains it — drop the shorter.
  Equal-length or diverged spines stay a `conflict` and get a `.incoming` file.
- **Never auto-promote `.incoming`.** Measured on real data: some were larger only
  because they carried unrewritten paths, and one was 159 lines shorter than the file it
  would have replaced.
- `install()` installs a **skill**, not a slash command, and warns if a legacy
  `~/.claude/commands/migrate.md` still exists. Two artifacts named `migrate` drift.
- `CLAUDE_CONFIG_DIR` overrides `~/.claude` — use it to test `install` without touching
  your real config.

## Install / dev

- Plugin: `/plugin marketplace add FarisHijazi/claude-migrate` then
  `/plugin install claude-migrate@farishijazi`
- CLI: `uvx git+https://github.com/FarisHijazi/claude-migrate <cmd>` —
  **always the git URL.** The PyPI build lags and ships no `export`/`import`/`sync`.
- Dev: `uv sync`; tests `uv run --with pytest pytest`; build check `uv build --wheel`
