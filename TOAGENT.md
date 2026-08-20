# Agent Entry Point

Read AGENTS.md first, then follow the single procedure in
docs/OPERATING_GUIDE.md. Do not infer permissions from this page.

The required entry checks are:

~~~bash
git status --short --branch
git remote -v
git rev-parse HEAD
uv run --frozen bmp-agent validate
uv run --frozen bmp-collab validate
uv run --frozen bmp-collab modes
uv run --frozen bmp-lab doctor
uv run --frozen bmp-lab status --format json
~~~

Declare role, issue, base commit, isolated branch/worktree, exact write scope,
held-fixed behavior, acceptance checks, and recovery artifacts before edits.
Claim the scope through bmp-lab; never hand-edit immutable lab JSON. Use a
fresh durable record root for every execution. Preserve failures and invalid
results, and never derive claim eligibility from logs or receipts.

PoorOtterBob is the sole accountable repository reviewer. Stop when identity,
lease, runtime, verifier, provenance, or live-job state cannot be proven.
