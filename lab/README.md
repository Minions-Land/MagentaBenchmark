# MagentaBench Lab Ledger

This directory is the machine-owned collaboration ledger for recoverable
benchmark work:

~~~text
lab/issues/<issue-id>/issue.json
lab/issues/<issue-id>/events/<event-id>.json
lab/.lab.lock                         # local, ignored mutex
~~~

Create and mutate records only with `uv run --frozen bmp-lab ...`. Do not hand-edit,
delete, rename, move, or resequence issue/event JSON. The live board is derived
with `uv run --frozen bmp-lab status`; no checked-in global status file should duplicate
it. Commit immutable issue/event records to Git, but never store credentials,
tokens, private authenticated URLs, or other secret values here.

Follow docs/OPERATING_GUIDE.md for leases,
checkpoints, recovery, multi-host Git coordination, and the exact boundary
between collaboration state and benchmark evidence.

## Initial real-experiment ledger

The first five issues were backfilled from facts retained in the repository and
local infrastructure checks. All currently reduce to `blocked`; none links a
successful real benchmark run:

- `tb-pinned-images`: the exact `regex-log:20251031` and
  `headless-terminal:20251031` Docker images are absent.
- `tb-container-verifier-uvx`: the retained task-container verifier could not
  download `uv` and then lacked `uvx`; it depends on the pinned images.
- `magenta-activation-usage`: the provider binding is not frozen and no current
  real record proves matching activation plus observable token/cost usage.
- `magenta-single-case-pilot`: the single explicit `fix-git` exploratory pilot
  awaits all three prerequisites and a deliberate nonzero token budget.
- `magenta-repeated-sampling`: sampling awaits a reproducible,
  standalone-verified pilot before its denominator or repetitions are chosen.

This list is only the bootstrap inventory. Use
`uv run --frozen bmp-lab status` and
`uv run --frozen bmp-lab show <issue-id>` for current reduced state.
