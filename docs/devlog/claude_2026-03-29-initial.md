# claude-migrate — initial implementation

**Date:** 2026-03-29

## What

Created `claude-migrate` as a pip-installable Python CLI tool that copies Claude Code conversation history when moving project directories.

## Key discovery

Claude Code encodes project paths by replacing **both** `/` and `.` with `-`. The gist reference only mentioned `/`, but testing against real encoded dirs confirmed `.` is also replaced. E.g.:
- `/Users/foo/.claude` → `-Users-foo--claude`
- `/Users/foo/acme.d` → `-Users-foo-acme-d`

## Features

- Copies `~/.claude/projects/<old-encoded>/` → `<new-encoded>/`
- Appends a user message to the latest session JSONL noting the path change
- `--dry-run` to preview
- Refuses to overwrite existing history dirs

## Install methods

- `uv tool install ~/claude-migrate` — global CLI
- `pip install ~/claude-migrate`
- Claude Code slash command: `/migrate <new_path>`
- Direct in Claude Code: `! claude-migrate "$(pwd)" /new/path`

## Reference

Based on: https://gist.github.com/gwpl/e0b78a711b4a6b2fc4b594c9b9fa2c4c
