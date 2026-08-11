#!/usr/bin/env bash

set -uo pipefail

unset RIPGREP_CONFIG_PATH || {
  echo "BMP-HCP-AUDIT: cannot clear RIPGREP_CONFIG_PATH" >&2
  exit 2
}

SCRIPT_PATH="${BASH_SOURCE[0]}"
case "$SCRIPT_PATH" in
  */*) SCRIPT_PARENT="${SCRIPT_PATH%/*}" ;;
  *) SCRIPT_PARENT="." ;;
esac
SCRIPT_DIR="$(cd -- "$SCRIPT_PARENT" && pwd -P)" || {
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

declare -a INCLUDE_SUFFIXES=(
  '.py' '.pyi' '.ts' '.tsx' '.js' '.mjs' '.cjs'
  '.json' '.toml' '.yaml' '.yml'
)
declare -a INCLUDE_GLOBS=(
  -g '*.py' -g '*.pyi' -g '*.ts' -g '*.tsx' -g '*.js' -g '*.mjs'
  -g '*.cjs' -g '*.json' -g '*.toml' -g '*.yaml' -g '*.yml'
)
declare -a EXCLUDE_ROOT_GLOBS=(
  '.git' 'docs' '.docs'
  'build' 'develop-eggs' 'dist' 'downloads' 'eggs' '.eggs'
  'lib' 'lib64' 'parts' 'sdist' 'var' 'wheels' '*.egg-info'
  'node_modules'
  '.venv' '.venv*' 'venv' 'ENV' 'env'
  '__pycache__' '.pytest_cache' 'htmlcov' '.tox' '.hypothesis'
  '.mypy_cache' '.ruff_cache' '.vscode' '.idea'
  '.runs' 'jobs' '.cache' 'vendor' 'third_party'
)
declare -a EXCLUDE_GLOBS=()
for exclude_root_glob in "${EXCLUDE_ROOT_GLOBS[@]}"; do
  EXCLUDE_GLOBS+=(-g "!${exclude_root_glob}/**")
done
declare -a CONTENT_RULES=(
  'BMP-HCP-B01' '\b(?:HcpBenchmarkServer|HcpEvaluator)\b'
  'BMP-HCP-B02' '\b(?:class|interface|type|enum|protocol|struct)\s+Hcp[A-Za-z0-9_]*\b'
  'BMP-HCP-B03' '\b(?:HcpClient|HcpServer|HcpMagnet)\b'
  'BMP-HCP-B04' '\b(?:toHcpServer|toTool|toCapability|toResource|registerModule)\s*\('
  'BMP-HCP-B05' '\b(?:HCP_SERVERS|HCP_MAGNETS)\b|sources\.generated\.ts|generate-hcp-sources'
  'BMP-HCP-B06' '(?i)\b(?:hcp|magenta_hcp)[_-]?(?:module|source|server|magnet)[_-]?(?:registry|map|list|table|builders?|defaults?)\b'
  'BMP-HCP-B07' '(?i)\bhcp[_-]?(?:product[_-]?)?(?:builder|factory|selector|switch)(?:s|_map|_table)?\b'
  'BMP-HCP-B08' '\b(?:ModuleHcpServer|CapabilityHcpServer|UniversalMagnet|ProcessToolMagnet|PythonModuleToolMagnet|HcpProcessMagnet|CapabilitySourceMagnet|HcpRequest|HcpResponse|HcpContext|HcpResource)\b'
  'BMP-HCP-B09' '(?i)\bhcp[_-]?(?:tool[_-]?call[_-]?)?middleware\b|\bHcp(?:ToolCall)?Middleware\b'
  'BMP-HCP-B10' '(?:from\s+|import\s*\()\s*["'"'"'][^"'"'"']*HarnessComponentProtocol/(?:\.HCP|_magenta|HcpClient\.ts|[^"'"'"']+/Hcp(?:Server|Magnet)\.ts)'
  'BMP-HCP-B11' '(?m)^\s*(?:from|import)\s+(?:Magenta\.)?HarnessComponentProtocol(?:\.|/)'
)
declare -a PATH_RULES=(
  'BMP-HCP-B12' '(?:^|/)(?:HarnessComponentProtocol|\.HCP)/(?:modules|hcp-client|hcp-contract|hcp-magnet|magnet|contract)(?:/|$)'
  'BMP-HCP-B13' '(?:^|/)Hcp(?:Client|Server|Magnet)\.(?:ts|tsx|js|mjs|cjs|py)$'
)

supports_pcre2() {
  local candidate="$1"
  printf 'pcre\n' | "$candidate" --pcre2 -q '(?<=p)cre' \
    >/dev/null 2>&1
}

RG_BIN="$(type -P rg || true)"
if [[ -n "$RG_BIN" ]] && ! supports_pcre2 "$RG_BIN"; then
  RG_BIN=""
fi

if [[ -z "$RG_BIN" ]]; then
  UV_BIN="$(type -P uv || true)"
  if [[ -z "$UV_BIN" ]]; then
    echo \
      "BMP-HCP-AUDIT: rg with PCRE2 is required; managed bootstrap requires uv" \
      >&2
    exit 2
  fi

  MANAGED_RG_SPEC='ripgrep-bin==15.1.0'
  managed_rg_output="$("$UV_BIN" --no-config tool run --isolated --no-build \
    --from "$MANAGED_RG_SPEC" sh -c 'command -v rg')"
  managed_rg_status=$?
  if (( managed_rg_status != 0 )); then
    echo "BMP-HCP-AUDIT: failed to bootstrap $MANAGED_RG_SPEC with uv" >&2
    exit 2
  fi
  RG_BIN="${managed_rg_output%%$'\n'*}"
  if [[ -z "$RG_BIN" || ! -x "$RG_BIN" ]]; then
    echo \
      "BMP-HCP-AUDIT: managed $MANAGED_RG_SPEC did not resolve an executable rg" \
      >&2
    exit 2
  fi
  managed_rg_version="$("$RG_BIN" --version 2>/dev/null)"
  case "$managed_rg_version" in
    'ripgrep 15.1.0 '*) ;;
    *)
      echo \
        "BMP-HCP-AUDIT: managed $MANAGED_RG_SPEC did not resolve ripgrep 15.1.0" \
        >&2
      exit 2
      ;;
  esac
  if ! supports_pcre2 "$RG_BIN"; then
    echo \
      "BMP-HCP-AUDIT: managed $MANAGED_RG_SPEC lacks working PCRE2 support" \
      >&2
    exit 2
  fi
fi

violations=0
scan_errors=0

FILE_LIST="$(mktemp)" || {
  echo "BMP-HCP-AUDIT: cannot create file-list output" >&2
  exit 2
}
FILE_ERRORS="$(mktemp)" || {
  echo "BMP-HCP-AUDIT: cannot create file-enumeration error output" >&2
  rm -f -- "$FILE_LIST"
  exit 2
}
trap 'rm -f -- "$FILE_LIST" "$FILE_ERRORS"' EXIT
(
  cd -- "$ROOT" || exit 2
  "$RG_BIN" --files --hidden --no-ignore \
    "${EXCLUDE_GLOBS[@]}" "$ROOT"
) >"$FILE_LIST" 2>"$FILE_ERRORS"
file_status=$?
if (( file_status != 0 )); then
  echo "BMP-HCP-AUDIT: file enumeration failed" >&2
  cat "$FILE_ERRORS" >&2
  scan_errors=$((scan_errors + 1))
fi

preflight_utf8() {
  local python_executable="$1"
  "$python_executable" -I -S - "$FILE_LIST" \
    "${INCLUDE_SUFFIXES[@]}" <<'PY'
from pathlib import Path
import sys


try:
    file_list = Path(sys.argv[1]).read_bytes().decode("utf-8").splitlines()
except (OSError, UnicodeError) as exc:
    print(
        f"BMP-HCP-AUDIT scan error: cannot read UTF-8 file list: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1)

suffixes = tuple(sys.argv[2:])
errors = 0
for rendered in file_list:
    if not rendered.endswith(suffixes):
        continue
    path = Path(rendered)
    try:
        path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        print(
            f"BMP-HCP-AUDIT scan error: cannot read UTF-8 source {path}: {exc}",
            file=sys.stderr,
        )
        errors += 1

raise SystemExit(1 if errors else 0)
PY
}

if (( file_status == 0 )); then
  PYTHON_BIN="$(type -P python3 || type -P python || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    echo \
      "BMP-HCP-AUDIT scan error: Python 3 is required for UTF-8 preflight" \
      >&2
    scan_errors=$((scan_errors + 1))
  elif ! preflight_utf8 "$PYTHON_BIN"; then
    scan_errors=$((scan_errors + 1))
  fi
fi

audit_content() {
  local rule="$1"
  local pattern="$2"
  local output status line
  output="$(mktemp)" || {
    echo "$rule scan error: cannot create temporary output" >&2
    scan_errors=$((scan_errors + 1))
    return
  }
  (
    cd -- "$ROOT" || exit 2
    "$RG_BIN" --hidden --no-ignore --text --encoding utf-8 --pcre2 \
      -n --no-heading --color never \
      "${INCLUDE_GLOBS[@]}" "${EXCLUDE_GLOBS[@]}" \
      -- "$pattern" "$ROOT"
  ) >"$output" 2>&1
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
  "$RG_BIN" --text --pcre2 -n --no-heading --color never \
    -- "$pattern" "$FILE_LIST" >"$output" 2>&1
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

for ((index = 0; index < ${#CONTENT_RULES[@]}; index += 2)); do
  audit_content "${CONTENT_RULES[index]}" "${CONTENT_RULES[index + 1]}"
done

if (( file_status == 0 )); then
  for ((index = 0; index < ${#PATH_RULES[@]}; index += 2)); do
    audit_path "${PATH_RULES[index]}" "${PATH_RULES[index + 1]}"
  done
fi

printf 'BMP-HCP-AUDIT: %d violation(s), %d scan error(s)\n' \
  "$violations" "$scan_errors"
if (( violations > 0 || scan_errors > 0 )); then
  exit 1
fi
