# claude-migrate

CLI + Claude Code plugin for moving conversation history when a project moves — to a new
path, to another machine, or continuously between two machines. Optionally moves the
project folder too, and rewrites path references inside the chat content itself.

## Structure

```
.claude-plugin/
  plugin.json          # plugin manifest
  marketplace.json     # the repo is its own marketplace ("farishijazi")
skills/
  migrate -> ../src/claude_migrate/skill      # SYMLINK, see below
src/claude_migrate/
  cli.py               # subcommands, encode_path, smart_merge_file, content
                       #   rewriting, export/import(-all), user-messages, install
  sync.py              # `sync`: two-way peer sync. Imported LAZILY (needs fcntl,
                       #   which does not exist on Windows)
  skill/               # the /migrate skill — THE canonical copy. There is no
                       #   migrate.md slash command any more; the skill replaced it
    SKILL.md
    references/platform-notes.md
tests/
  smart_merge_test.py  # merge semantics — uv run --with pytest pytest
  cli_features_test.py # content rewriting, export-all/import-all, user-messages, install
```

**The skill exists once, at `src/claude_migrate/skill/`, and `skills/migrate` is a
symlink to it.** Both distribution channels need the same files in different places: the
plugin loader reads `skills/` from a git checkout (symlinks fine), while the wheel needs
real files under the package for `importlib.resources`. The symlink must point that
direction and not the other — **`uv_build` refuses to follow a symlinked directory** and
fails the build with `Is a directory (os error 21)`. Verified by building the wheel and
listing it; do that again if you ever move these files.

Both halves are verified, not assumed: the wheel was built and listed to confirm it holds
real files, and a symlinked skill directory was dropped into `~/.claude/skills/` to
confirm the loader follows it. **Known limit:** a Windows checkout without
`core.symlinks` writes the link as a text file, so the plugin's skill would not load
there. The `uvx … install-skill` path still works on Windows, since the wheel carries
real files.

## CLI surface

Primary command:
- `migrate <old> <new>` — migrate history; flags below
  - `--merge` / `-m` — smart-merge into existing destination history
  - `--folder` — also copy the project folder (errors if destination folder exists)
  - `--delete-history` — delete source history (move semantics for history)
  - `--delete-dir` — delete source folder (requires `--folder`)
  - `--replace-userpath[=FROM:TO]` — rewrite `/Users/<x>/`, `/home/<x>/` and encoded forms
    in chat content; bare flag auto-detects, `=FROM:TO` overrides
  - `--replace-references` — rewrite absolute paths under the old CWD and relative paths
    that escape it; inside-CWD relatives are kept as-is
  - `--dry-run` / `-n`

Other commands:
- `rm <path>` — remove history; `--folder` also deletes the project folder
- `export <project_path>` / `export-all` — archive to `.tar.gz`
- `import <archive> [target]` (alias `merge`) / `import-all <archive>` — restore from
  archive; both accept `--replace-userpath` and `--replace-references`
- `sync` — two-way peer sync (see below)
- `user-messages` — extract human-typed messages from the current session JSONL; flags
  `--copy`, `-o FILE`, `--session FILE`, `--include-meta`, `--separator STR`
- `install-skill` (alias `install-slash-command`) — install the `/migrate` skill
- `cp` / `mv` — deprecated aliases of `migrate` and `migrate --delete-history`

## Content rewriting internals

`rewrite_history_content` walks every JSON/JSONL file and parses each line. Every string
value (recursively) goes through `transform_string`, which applies
`apply_userpath_to_string` then `apply_references_to_string`. The regex uses a
positive-lookahead boundary class so `/old/cwd` cannot corrupt `/old/cwdbar`. Each unique
`before -> after` pair is printed before the write.

