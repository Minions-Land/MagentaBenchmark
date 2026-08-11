# Experiment Bundles

Each directory below this one is an independently mergeable collaboration
unit. `bundle.json` pins one BMP declaration and `PLAN.md` records the human
question, hypothesis, and stop conditions. The live owner, blockers, lease,
checkpoint, and run state are in `lab/`; do not add a status column here.

Use the agent entrypoints from the repository root:

```bash
uv run bmp-collab validate
uv run bmp-collab next
uv run bmp-collab scaffold <id> --bmp-spec <path> --lab-issue <id> \
  --question "..." --hypothesis "..." --stop-condition "..."
```

The bundle overlay does not replace or silently edit BMP protocol declarations.
An experiment-only PR should normally touch only its own directory, its
immutable lab issue/event records, and focused tests. See
[`docs/EXPERIMENT_COLLABORATION.md`](../docs/EXPERIMENT_COLLABORATION.md) for
execution modes, adapter extension rules, and recovery boundaries.
