#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || {
  echo "BMP-HCP-AUDIT: cannot resolve script directory" >&2
  exit 2
}
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)" || {
  echo "BMP-HCP-AUDIT: cannot resolve repository root" >&2
  exit 2
}

if [[ ! -d "$ROOT/MagentaBench" ]]; then
  echo "BMP-HCP-AUDIT: missing scan root: $ROOT/MagentaBench" >&2
  exit 2
fi
if ! command -v rg >/dev/null 2>&1; then
  echo "BMP-HCP-AUDIT: rg is required" >&2
  exit 2
fi
if ! printf 'pcre\n' | rg --pcre2 -q '(?<=p)cre'; then
  echo "BMP-HCP-AUDIT: rg with PCRE2 support is required" >&2
  exit 2
fi

declare -a INCLUDE_GLOBS=(
  -g '*.py' -g '*.pyi' -g '*.ts' -g '*.tsx' -g '*.js' -g '*.mjs'
  -g '*.cjs' -g '*.json' -g '*.toml' -g '*.yaml' -g '*.yml'
)
declare -a EXCLUDE_GLOBS=(
  -g '!.git/**' -g '!docs/**' -g '!.docs/**'
  -g '!build/**' -g '!dist/**' -g '!node_modules/**'
  -g '!.venv/**' -g '!.venv*/**' -g '!venv/**' -g '!env/**'
  -g '!__pycache__/**' -g '!.pytest_cache/**' -g '!.tox/**'
  -g '!.mypy_cache/**' -g '!.ruff_cache/**' -g '!vendor/**'
  -g '!third_party/**'
)

violations=0
scan_errors=0

audit_content() {
  local rule="$1"
  local pattern="$2"
  local output status line
  output="$(mktemp)" || {
    echo "$rule scan error: cannot create temporary output" >&2
    scan_errors=$((scan_errors + 1))
    return
  }
  rg --hidden --pcre2 -n --no-heading --color never \
    "${INCLUDE_GLOBS[@]}" "${EXCLUDE_GLOBS[@]}" \
    -- "$pattern" "$ROOT" >"$output" 2>&1
  status=$?
  if (( status == 0 )); then
    while IFS= read -r line; do
      printf '%s %s\n' "$rule" "$line"
      violations=$((violations + 1))
    done <"$output"
  elif (( status > 1 )); then
    while IFS= read -r line; do
      printf '%s scan error: %s\n' "$rule" "$line" >&2
    done <"$output"
    scan_errors=$((scan_errors + 1))
  fi
  rm -f -- "$output"
}

audit_path() {
  local rule="$1"
  local pattern="$2"
  local output status line
  output="$(mktemp)" || {
    echo "$rule scan error: cannot create temporary output" >&2
    scan_errors=$((scan_errors + 1))
    return
  }
  rg --pcre2 -n --no-heading --color never -- "$pattern" "$FILE_LIST" \
    >"$output" 2>&1
  status=$?
  if (( status == 0 )); then
    while IFS= read -r line; do
      printf '%s %s\n' "$rule" "$line"
      violations=$((violations + 1))
    done <"$output"
  elif (( status > 1 )); then
    while IFS= read -r line; do
      printf '%s scan error: %s\n' "$rule" "$line" >&2
    done <"$output"
    scan_errors=$((scan_errors + 1))
  fi
  rm -f -- "$output"
}

audit_content 'BMP-HCP-B01' '\b(?:HcpBenchmarkServer|HcpEvaluator)\b'
audit_content 'BMP-HCP-B02' '\b(?:class|interface|type|enum|protocol|struct)\s+Hcp[A-Za-z0-9_]*\b'
audit_content 'BMP-HCP-B03' '\b(?:HcpClient|HcpServer|HcpMagnet)\b'
audit_content 'BMP-HCP-B04' '\b(?:toHcpServer|toTool|toCapability|toResource|registerModule)\s*\('
audit_content 'BMP-HCP-B05' '\b(?:HCP_SERVERS|HCP_MAGNETS)\b|sources\.generated\.ts|generate-hcp-sources'
audit_content 'BMP-HCP-B06' '(?i)\b(?:hcp|magenta_hcp)[_-]?(?:module|source|server|magnet)[_-]?(?:registry|map|list|table|builders?|defaults?)\b'
audit_content 'BMP-HCP-B07' '(?i)\bhcp[_-]?(?:product[_-]?)?(?:builder|factory|selector|switch)(?:s|_map|_table)?\b'
audit_content 'BMP-HCP-B08' '\b(?:ModuleHcpServer|CapabilityHcpServer|UniversalMagnet|ProcessToolMagnet|PythonModuleToolMagnet|HcpProcessMagnet|CapabilitySourceMagnet|HcpRequest|HcpResponse|HcpContext|HcpResource)\b'
audit_content 'BMP-HCP-B09' '(?i)\bhcp[_-]?(?:tool[_-]?call[_-]?)?middleware\b|\bHcp(?:ToolCall)?Middleware\b'
audit_content 'BMP-HCP-B10' '(?:from\s+|import\s*\()\s*["'"'"'][^"'"'"']*HarnessComponentProtocol/(?:\.HCP|_magenta|HcpClient\.ts|[^"'"'"']+/Hcp(?:Server|Magnet)\.ts)'
audit_content 'BMP-HCP-B11' '(?m)^\s*(?:from|import)\s+(?:Magenta\.)?HarnessComponentProtocol(?:\.|/)'

FILE_LIST="$(mktemp)" || {
  echo "BMP-HCP-AUDIT: cannot create file-list output" >&2
  exit 2
}
trap 'rm -f -- "$FILE_LIST"' EXIT
rg --files --hidden "${EXCLUDE_GLOBS[@]}" "$ROOT" >"$FILE_LIST" 2>&1
file_status=$?
if (( file_status != 0 )); then
  echo "BMP-HCP-AUDIT: file enumeration failed" >&2
  cat "$FILE_LIST" >&2
  scan_errors=$((scan_errors + 1))
else
  audit_path 'BMP-HCP-B12' '(?:^|/)(?:HarnessComponentProtocol|\.HCP)/(?:modules|hcp-client|hcp-contract|hcp-magnet|magnet|contract)(?:/|$)'
  audit_path 'BMP-HCP-B13' '(?:^|/)Hcp(?:Client|Server|Magnet)\.(?:ts|tsx|js|mjs|cjs|py)$'
fi

printf 'BMP-HCP-AUDIT: %d violation(s), %d scan error(s)\n' \
  "$violations" "$scan_errors"
if (( violations > 0 || scan_errors > 0 )); then
  exit 1
fi

