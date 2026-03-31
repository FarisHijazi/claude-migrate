---
argument-hint: <new_path>
description: Migrate conversation history after moving this project to a new directory
model: claude-haiku
---

# Migrate Claude Code History

The user has moved (or is about to move) this project to a new directory. Run the migration script to copy the conversation history so `claude --continue` works at the new location.

**Old path (current):** The current working directory (use `pwd`)
**New path:** $ARGUMENTS

Run this command:
```
uvx claude-migrate cp "$(pwd)" "$ARGUMENTS"
```

After running, tell the user:
1. The history has been copied
2. They can now `cd $ARGUMENTS && claude --continue` to resume
