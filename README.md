# claude-migrate

Copy Claude Code conversation history when moving project directories.

When you `mv` a project, Claude Code loses `--continue` history because it's keyed by the absolute path. This tool copies `~/.claude/projects/<old-encoded>/` to `~/.claude/projects/<new-encoded>/` and appends a migration notice to the latest session.

## Quick start

Install the `/migrate` slash command (one-time):

```bash
# install `uv` if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh
uvx claude-migrate install
```

Then inside Claude Code:

```
/migrate /new/path
```

## Usage

### From inside Claude Code

Using the `!` prefix (no AI overhead):

```
! uvx claude-migrate copy "$(pwd)" /new/path
```

Or use the `/migrate` slash command (after running `install`):

```
/migrate /new/path
```

### From the terminal

```bash
# Preview what would happen
uvx claude-migrate copy --dry-run /old/path /new/path

# Copy history to match new location
uvx claude-migrate copy /old/path /new/path

# Then continue at the new location
cd /new/path && claude --continue
```

## How it works

1. Claude Code encodes project paths by replacing `/` and `.` with `-` (e.g. `/home/user/project` -> `-home-user-project`)
2. History lives at `~/.claude/projects/<encoded-path>/` as JSONL files
3. This tool copies that directory to the new encoded path
4. Appends a user message to the latest session noting the path change, so Claude knows files moved

## Reference

Based on: https://gist.github.com/gwpl/e0b78a711b4a6a2fc4b594c9b9fa2c4c
