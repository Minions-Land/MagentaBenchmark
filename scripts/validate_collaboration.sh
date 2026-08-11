#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$ROOT"

printf '[collaboration] validating bundles, BMP pins, backend modes, and lab links\n'
uv run --frozen bmp-collab validate
printf '[collaboration] execution target inventory\n'
uv run --frozen bmp-collab modes
printf '[collaboration] lab ledger\n'
uv run --frozen bmp-lab doctor
