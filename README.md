# MagentaBench

MagentaBench is the benchmark-side protocol and evidence ledger for
reproducible agent evaluation. It binds a benchmark, subject, execution
backend, case set, evaluator, report, and durable artifacts without replacing
the agent framework or the benchmark's official verifier.

## Start Here

The single operational workflow is
[docs/OPERATING_GUIDE.md](docs/OPERATING_GUIDE.md). It covers:

- GitHub Issues, pull requests, review, and merge authority
- branch and worktree ownership, leases, integration, and retirement
- source snapshots, historical imports, and case-level result rows
- experiment bundles, preregistration, execution targets, and budgets
- fresh record roots, independent verification, ledger linking, and claims
- checkpoints, recovery, and shift handoffs

Read [AGENTS.md](AGENTS.md) before changing anything. AGENTS.md is the
non-negotiable authority boundary; the guide is the shared operating
procedure. PoorOtterBob is the sole accountable repository reviewer.

## Minimal Entry Check

Run from the repository root:

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

Use origin for canonical GitHub synchronization. A configured mirror is
fetch-only acceleration. Never store credentials, authenticated URLs, private
paths, or transient .tmp content in Git or evidence.

## Evidence Boundary

An experiment declaration is not a result. Every run uses a fresh durable
record root and retains all planned cases, including failures, timeouts,
invalid results, and missing outputs. The official verifier reads persisted
bytes; logs and process exit codes are diagnostic only. A claim can be
published only through the BMP verifier and ledger gates.

The protocol and data contracts remain in their existing governance, ledger,
historical-import, and result-authority files. They define schemas and facts;
the operating guide defines how to use them.
