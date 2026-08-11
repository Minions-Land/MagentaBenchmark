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

run_python_fallback() {
  local python_executable="$1"
  "$python_executable" -I -S - "$ROOT" \
    --suffixes "${INCLUDE_SUFFIXES[@]}" \
    --exclude-root-globs "${EXCLUDE_ROOT_GLOBS[@]}" \
    --content-rules "${CONTENT_RULES[@]}" \
    --path-rules "${PATH_RULES[@]}" <<'PY'
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
import stat
import sys


def sections(values: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    current: str | None = None
    for value in values:
        if value.startswith("--"):
            current = value[2:]
            if current in result:
                raise ValueError(f"duplicate section {value}")
            result[current] = []
        elif current is None:
            raise ValueError(f"value before section: {value!r}")
        else:
            result[current].append(value)
    return result


def pairs(values: list[str], *, label: str) -> list[tuple[str, str]]:
    if len(values) % 2:
        raise ValueError(f"{label} must contain rule/pattern pairs")
    return list(zip(values[::2], values[1::2], strict=True))


def excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


violations = 0
scan_errors: list[str] = []
try:
    root = Path(sys.argv[1]).resolve(strict=True)
    configured = sections(sys.argv[2:])
    suffixes = tuple(configured["suffixes"])
    exclude_root_globs = configured["exclude-root-globs"]
    content_rules = [
        (rule, re.compile(pattern))
        for rule, pattern in pairs(configured["content-rules"], label="content rules")
    ]
    path_rules = [
        (rule, re.compile(pattern))
        for rule, pattern in pairs(configured["path-rules"], label="path rules")
    ]
except (KeyError, OSError, re.error, ValueError) as exc:
    print(f"BMP-HCP-AUDIT scan error: invalid fallback configuration: {exc}", file=sys.stderr)
    print("BMP-HCP-AUDIT: 0 violation(s), 1 scan error(s)")
    raise SystemExit(1)


def walk_error(error: OSError) -> None:
    scan_errors.append(f"cannot enumerate {error.filename}: {error}")


files: list[Path] = []
for directory, child_directories, child_files in os.walk(
    root, topdown=True, onerror=walk_error, followlinks=False
):
    directory_path = Path(directory)
    child_directories[:] = sorted(
        name
        for name in child_directories
        if not (
            directory_path == root and excluded(name, exclude_root_globs)
        )
        and not (directory_path / name).is_symlink()
    )
    for name in sorted(child_files):
        path = directory_path / name
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            scan_errors.append(f"cannot inspect {path}: {exc}")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            scan_errors.append(f"scan candidate is not a regular file: {path}")
            continue
        files.append(path)

files.sort(key=lambda path: path.as_posix())
source_lines: dict[Path, tuple[str, ...]] = {}
for path in files:
    if not path.name.endswith(suffixes):
        continue
    try:
        source_lines[path] = tuple(
            path.read_bytes().decode("utf-8-sig").split("\n")
        )
    except (OSError, UnicodeError) as exc:
        scan_errors.append(f"cannot read UTF-8 source {path}: {exc}")

for rule, pattern in content_rules:
    for path, lines in source_lines.items():
        for line_number, line in enumerate(lines, start=1):
            if pattern.search(line) is not None:
                print(f"{rule} {path}:{line_number}:{line}")
                violations += 1

for rule, pattern in path_rules:
    for path in files:
        rendered = path.as_posix()
        if pattern.search(rendered) is not None:
            print(f"{rule} {rendered}:1:{rendered}")
            violations += 1

for message in scan_errors:
    print(f"BMP-HCP-AUDIT scan error: {message}", file=sys.stderr)
print(
    f"BMP-HCP-AUDIT: {violations} violation(s), {len(scan_errors)} scan error(s)"
)
raise SystemExit(1 if violations or scan_errors else 0)
PY
}

if ! command -v rg >/dev/null 2>&1 \
  || ! printf 'pcre\n' | rg --pcre2 -q '(?<=p)cre'; then
  python_executable="$(command -v python3 || command -v python || true)"
  if [[ -z "$python_executable" ]]; then
    echo "BMP-HCP-AUDIT: rg with PCRE2 or Python 3 is required" >&2
    exit 2
  fi
  run_python_fallback "$python_executable"
  exit $?
fi

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
  (
    cd -- "$ROOT" || exit 2
    rg --hidden --no-ignore --text --pcre2 -n --no-heading --color never \
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

for ((index = 0; index < ${#CONTENT_RULES[@]}; index += 2)); do
  audit_content "${CONTENT_RULES[index]}" "${CONTENT_RULES[index + 1]}"
done

FILE_LIST="$(mktemp)" || {
  echo "BMP-HCP-AUDIT: cannot create file-list output" >&2
  exit 2
}
trap 'rm -f -- "$FILE_LIST"' EXIT
(
  cd -- "$ROOT" || exit 2
  rg --files --hidden --no-ignore "${EXCLUDE_GLOBS[@]}" "$ROOT"
) >"$FILE_LIST" 2>&1
file_status=$?
if (( file_status != 0 )); then
  echo "BMP-HCP-AUDIT: file enumeration failed" >&2
  cat "$FILE_LIST" >&2
  scan_errors=$((scan_errors + 1))
else
  for ((index = 0; index < ${#PATH_RULES[@]}; index += 2)); do
    audit_path "${PATH_RULES[index]}" "${PATH_RULES[index + 1]}"
  done
fi

printf 'BMP-HCP-AUDIT: %d violation(s), %d scan error(s)\n' \
  "$violations" "$scan_errors"
if (( violations > 0 || scan_errors > 0 )); then
  exit 1
fi
