#!/usr/bin/env bash

# Run the cheap, deterministic checks that must precede an experiment.
# This script deliberately does not execute a benchmark or create a record root.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

usage() {
  printf 'usage: %s EXPERIMENT.toml\n' "$(basename "$0")" >&2
  printf '       EXPERIMENT.toml must be inside the MagentaBench project root.\n' >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

experiment_input="$1"
if [[ "$experiment_input" != /* && ! -f "$experiment_input" && -f "$ROOT/$experiment_input" ]]; then
  experiment_input="$ROOT/$experiment_input"
fi
if [[ ! -f "$experiment_input" ]]; then
  printf '[preflight] experiment not found: %s\n' "$experiment_input" >&2
  exit 2
fi
if ! EXPERIMENT="$(realpath -- "$experiment_input" 2>/dev/null)"; then
  printf '[preflight] cannot resolve experiment path: %s\n' "$experiment_input" >&2
  exit 2
fi
if ! relative_experiment="$(realpath --relative-to="$ROOT" "$EXPERIMENT" 2>/dev/null)"; then
  printf '[preflight] cannot relativize experiment path: %s\n' "$EXPERIMENT" >&2
  exit 2
fi
if [[ "$relative_experiment" == ../* || "$relative_experiment" == ".." ]]; then
  printf '[preflight] experiment must be inside project root: %s\n' "$EXPERIMENT" >&2
  exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '[preflight] uv is required (set UV_DEFAULT_INDEX for a package mirror)\n' >&2
  exit 2
fi

cd -- "$ROOT"
printf '[preflight] project: %s\n' "$ROOT"
printf '[preflight] experiment: %s\n' "$relative_experiment"

printf '[preflight] verifying registry lock...\n'
uv run python - "$ROOT" <<'PY'
from pathlib import Path
import sys

from MagentaBench.schemas.registry_lock import verify_registry_lock

catalog = verify_registry_lock(Path(sys.argv[1]) / "registries")
print(f"[preflight] registry lock OK: {len(catalog.entries)} entries, {catalog.catalog_digest}")
PY

printf '[preflight] compiling manifest...\n'
compile_output="$(mktemp)"
trap 'rm -f -- "$compile_output"' EXIT
uv run bmp-compile "$EXPERIMENT" --project-root "$ROOT" >"$compile_output"
uv run python - "$compile_output" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    runs = json.load(handle)
if not runs:
    raise SystemExit("[preflight] compiler returned no runs")
print(f"[preflight] compile OK: {len(runs)} run(s)")
for run in runs:
    print(f"[preflight]   {run['run_id']} manifest={run['manifest_digest']}")
PY

printf '[preflight] compiling Python sources...\n'
uv run python -m compileall -q MagentaBench plugins tests
printf '[preflight] compileall OK\n'

printf '[preflight] checking patch whitespace...\n'
git -C "$ROOT" diff --check
printf '[preflight] git diff --check OK\n'

purpose="$(uv run python - "$EXPERIMENT" <<'PY'
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

with open(sys.argv[1], "rb") as handle:
    document = tomllib.load(handle)
print(document.get("experiment", {}).get("design", {}).get("purpose", "unknown"))
PY
)"
case "$purpose" in
  claim)
    printf '[preflight] WARNING: purpose=claim. This only clears preflight; publish requires positive validity gates and standalone report verification.\n'
    ;;
  exploratory)
    printf '[preflight] WARNING: purpose=exploratory. Results must not be presented as a claim or leaderboard score.\n'
    ;;
  *)
    printf '[preflight] WARNING: experiment purpose=%s. Confirm the manifest before execution; unknown purpose is not claim-ready.\n' "$purpose"
    ;;
esac

printf '[preflight] READY: execute with bmp-run using a fresh record root, then verify the persisted report.\n'
