#!/usr/bin/env bash
# Run all CI quality gates (ruff lint, ruff format-check, mypy, pytest)
# sequentially. Exits non-zero on the first failing gate.
#
# Usage:
#   ./check.sh           # check-only mode (default)
#   ./check.sh --fix     # auto-fix ruff lint + format issues, then run gates
#   ./check.sh --help    # show usage
#
# This is the canonical pre-commit / pre-PR command — equivalent to
# `ruff check . && ruff format --check . && mypy && pytest` but with per-step
# progress markers and a single "all green" summary at the end.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer .venv/ when present (local dev), otherwise fall back to PATH (CI,
# system Python). This keeps `./check.sh` as the single source of truth for
# gates across both environments.
if [ -x ".venv/bin/python" ]; then
    readonly TOOL_PREFIX=".venv/bin/"
else
    readonly TOOL_PREFIX=""
fi

# Colour escape sequences only when stderr is a real terminal so they do not
# pollute CI logs or pipes.
if [ -t 2 ]; then
    readonly C_BLUE=$'\033[1;34m'
    readonly C_GREEN=$'\033[1;32m'
    readonly C_RED=$'\033[1;31m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_BLUE=''
    readonly C_GREEN=''
    readonly C_RED=''
    readonly C_RESET=''
fi

log() { printf '%s[check]%s %s\n' "$C_BLUE"  "$C_RESET" "$*" >&2; }
ok()  { printf '%s[check]%s %s\n' "$C_GREEN" "$C_RESET" "$*" >&2; }
err() { printf '%s[check]%s %s\n' "$C_RED"   "$C_RESET" "$*" >&2; }

usage() {
    cat <<'EOF'
Usage: ./check.sh [--fix] [-h|--help]

Run all CI gates (ruff check, ruff format --check, mypy, pytest) in order.
Stops at the first failure.

Options:
  --fix       Apply ruff lint auto-fixes and reformat in place before running
              the gates. Useful right before opening a PR.
  -h, --help  Show this message.
EOF
}

require_tools() {
    local missing=()
    for tool in ruff mypy pytest; do
        if ! command -v "${TOOL_PREFIX}${tool}" >/dev/null 2>&1; then
            missing+=("$tool")
        fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        return
    fi
    err "Missing dev tools: ${missing[*]}"
    if [ -z "$TOOL_PREFIX" ]; then
        err "Install them via 'pip install -e \".[dev]\"' or run './run.sh' to"
        err "bootstrap a project venv."
    else
        err "Run './run.sh' once to (re)install dev dependencies."
    fi
    exit 1
}

# Run a single gate. Wraps the command so we get a uniform pass/fail marker
# without losing the underlying tool's own output.
run_step() {
    local label="$1"; shift
    log "▶ ${label}"
    if ! "$@"; then
        err "✗ ${label} failed"
        exit 1
    fi
}

parse_args() {
    MODE="check"
    while [ $# -gt 0 ]; do
        case "$1" in
            --fix)      MODE="fix" ;;
            -h|--help)  usage; exit 0 ;;
            *)
                err "Unknown argument: $1"
                usage >&2
                exit 64
                ;;
        esac
        shift
    done
}

main() {
    parse_args "$@"
    require_tools

    if [ "$MODE" = "fix" ]; then
        log "Auto-fixing lint + formatting issues..."
        "${TOOL_PREFIX}ruff" check . --fix
        "${TOOL_PREFIX}ruff" format .
    fi

    run_step "ruff check"          "${TOOL_PREFIX}ruff" check .
    run_step "ruff format --check" "${TOOL_PREFIX}ruff" format --check .
    run_step "mypy"                "${TOOL_PREFIX}mypy"
    run_step "pytest"              "${TOOL_PREFIX}pytest"

    ok "All gates passed."
}

main "$@"
