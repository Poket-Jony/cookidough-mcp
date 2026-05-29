#!/usr/bin/env bash
# Bootstrap and run the Cookidough MCP server.
#
# - Detects a Python 3.12+ interpreter
# - Creates `.venv/` if missing
# - Installs the project (only when pyproject.toml is newer than the install
#   marker, so subsequent runs start instantly)
# - Loads `.env` (if present) into the environment
# - Validates required credentials and execs the server
#
# Any arguments to this script are forwarded to `cookidough-mcp`.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

readonly VENV_DIR=".venv"
readonly VENV_BIN="${VENV_DIR}/bin"
readonly INSTALL_MARKER="${VENV_DIR}/.install-stamp"

# Colour escape sequences are emitted only when stderr is a real terminal so
# they do not pollute log files or pipes.
if [ -t 2 ]; then
    readonly C_BLUE=$'\033[1;34m'
    readonly C_YELLOW=$'\033[1;33m'
    readonly C_RED=$'\033[1;31m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_BLUE=''
    readonly C_YELLOW=''
    readonly C_RED=''
    readonly C_RESET=''
fi

log()  { printf '%s[run]%s %s\n' "$C_BLUE"   "$C_RESET" "$*" >&2; }
warn() { printf '%s[run]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[run]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

# Print the path of the first Python ≥ 3.12 found, or return non-zero.
#
# When launched from a GUI (e.g. Claude Desktop on macOS) the inherited PATH
# is minimal (``/usr/bin:/bin:/usr/sbin:/sbin``) and Homebrew interpreters at
# ``/opt/homebrew/bin`` or ``/usr/local/bin`` are invisible to ``command -v``.
# We therefore probe well-known absolute paths in addition to PATH lookups,
# so a fresh clone bootstraps correctly under both shells and GUIs.
find_python() {
    local cmd path ver candidate
    for cmd in python3.14 python3.13 python3.12 python3 python; do
        for path in \
            "" \
            "/opt/homebrew/bin/" \
            "/usr/local/bin/" \
            "/opt/local/bin/" \
            "$HOME/.pyenv/shims/"
        do
            candidate="${path}${cmd}"
            if [ -z "$path" ]; then
                command -v "$cmd" >/dev/null 2>&1 || continue
                candidate=$(command -v "$cmd")
            else
                [ -x "$candidate" ] || continue
            fi
            ver=$("$candidate" -c 'import sys; print(sys.version_info[0]*100 + sys.version_info[1])' 2>/dev/null || true)
            if [[ "$ver" =~ ^[0-9]+$ ]] && [ "$ver" -ge 312 ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done
    return 1
}

ensure_venv() {
    if [ -x "${VENV_BIN}/python" ]; then
        return
    fi
    local python
    if ! python=$(find_python); then
        err "Python 3.12 or newer is required but was not found on PATH."
        err "Install it (e.g. 'brew install python@3.12' on macOS,"
        err "'apt install python3.12' on Debian/Ubuntu) and re-run."
        exit 1
    fi
    log "Creating virtual environment in ${VENV_DIR} ($("$python" --version 2>&1))"
    "$python" -m venv "$VENV_DIR"
}

# True when the project has never been installed or pyproject.toml was edited
# after the last successful install.
needs_install() {
    [ ! -f "$INSTALL_MARKER" ] || [ "pyproject.toml" -nt "$INSTALL_MARKER" ]
}

ensure_dependencies() {
    if ! needs_install; then
        return
    fi
    log "Installing project dependencies (this may take a moment)..."
    "${VENV_BIN}/pip" install --quiet --disable-pip-version-check --upgrade pip
    "${VENV_BIN}/pip" install --quiet --disable-pip-version-check -e .
    touch "$INSTALL_MARKER"
}

load_dotenv() {
    [ -f ".env" ] || return 0
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
}

assert_credentials() {
    local missing=()
    [ -z "${COOKIDOUGH_EMAIL:-}" ]    && missing+=("COOKIDOUGH_EMAIL")
    [ -z "${COOKIDOUGH_PASSWORD:-}" ] && missing+=("COOKIDOUGH_PASSWORD")
    if [ "${#missing[@]}" -eq 0 ]; then
        return
    fi
    err "Missing required environment variable(s): ${missing[*]}"
    if [ ! -f ".env" ] && [ -f ".env.example" ]; then
        warn "Hint: 'cp .env.example .env' and fill in your credentials, then re-run."
    fi
    exit 2
}

start_server() {
    log "Starting Cookidoo MCP server (transport: ${COOKIDOUGH_MCP_MODE:-stdio})"
    exec "${VENV_BIN}/cookidough-mcp" "$@"
}

main() {
    ensure_venv
    ensure_dependencies
    load_dotenv
    assert_credentials
    start_server "$@"
}

main "$@"
