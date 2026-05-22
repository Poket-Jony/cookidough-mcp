# AGENTS.md

Instructions for AI coding agents working in this repository. Agent-agnostic
by design — Claude Code, Cursor, Aider, OpenAI Codex CLI, and any other tool
that consumes [AGENTS.md](https://agents.md/) should follow what is described
here.

## Project overview

`cookidoo-mcp` is a Model Context Protocol (MCP) server that exposes the
Thermomix [Cookidoo](https://cookidoo.de) platform to LLM clients (Claude
Desktop, Claude Code, any MCP-aware tool). It consolidates four predecessor
projects into a single Python 3.12 codebase built on **FastMCP** and the
[`miaucl/cookidoo-api`](https://github.com/miaucl/cookidoo-api) library.

The full feature list and tool table is in [`README.md`](README.md).

## Setup commands

```bash
cp .env.example .env  # fill in COOKIDOO_EMAIL / COOKIDOO_PASSWORD
./run.sh              # idempotent: bootstrap venv + install + start server
```

`run.sh` skips the install step when `pyproject.toml` has not changed since
the last successful install, so subsequent runs start immediately. Any extra
arguments are forwarded to `cookidoo-mcp`.

Manual setup (without `run.sh`) is documented in `README.md` under the
"Development" section.

## Build, lint, and test commands

The project supports four quality gates that must all stay green. The
canonical way to run them is via the bundled script:

```bash
./check.sh           # run all gates, stop on first failure
./check.sh --fix     # auto-fix ruff lint + format, then run all gates
```

Equivalent manual invocation:

```bash
source .venv/bin/activate
ruff check .            # lint
ruff format --check .   # formatting
mypy                    # strict type checks over src/ and tests/
pytest                  # full test suite, coverage gate ≥ 80 %
```

Run `./check.sh` before committing. Coverage failures and any lint/type
errors block the build.

## Code style

- **Language**: All code, identifiers, docstrings, commit messages, and
  documentation are in **English**.
- **Linter / formatter**: `ruff` (see `pyproject.toml` for the active rules:
  `E F W I B UP SIM RUF N ANN PT C4 PIE RET PTH ASYNC DTZ LOG G T20 TID`).
- **Type checker**: `mypy --strict` over `src/` and `tests/`. Avoid
  `# type: ignore` — fix the type instead. The few existing ignores are
  documented at the call site.
- **Python idioms**: Python 3.12+ syntax (PEP 695 generics like
  `async def _run[T](...)`, `Self` return types, `|` unions, no
  `from __future__ import annotations` in code that needs runtime
  introspection by Pydantic/FastMCP).
- **Comments**: Default to none. Only add a comment when the **why** is
  non-obvious (a hidden constraint, a workaround for an upstream quirk).
  Never document what the code does — sprechende identifiers cover that.
- **Docstrings**: Only on public modules, classes, and tool functions. Tool
  docstrings are surfaced to the LLM client, so keep them short, precise, and
  outcome-focused.
- **Imports**: Inside `src/cookidoo_mcp/tools/*` relative imports from the
  parent package (`from ..context import ...`) are intentional and ignored
  by ruff's TID rule via per-file config.

## Testing instructions

- Async tests are the default (`asyncio_mode = "auto"` in `pyproject.toml`).
- `tests/conftest.py` provides a `FakeSession` that is statically guarded
  against `CookidooSessionProtocol`:

  ```python
  _PROTOCOL_GUARD: CookidooSessionProtocol = FakeSession()
  ```

  If you add a method to the protocol, the guard breaks at mypy time —
  update the fake in the same PR.
- Test the **behaviour**, not the implementation. Avoid tests that assert
  "mock was called" without verifying observable output.
- Private FastMCP API access is centralized in `tests/_mcp_internals.py`.
  If you need a tool function inside a test, route through there.
- New session methods need both a unit test (DTO mapping, error paths) and
  an integration-style test via `tests/test_session_methods.py`.

## Architecture

```
src/cookidoo_mcp/
├── config.py        # Pydantic-settings, env-driven Settings
├── constants.py     # Magic numbers / strings (timeouts, defaults)
├── context.py       # AppContext dataclass + ToolContext type alias
├── errors.py        # Domain exception hierarchy
├── models.py        # Pydantic DTOs for every tool I/O
├── session.py       # Repository facade over cookidoo-api + custom HTTP
├── transport.py     # Stdio / HTTP transport strategies
├── quality.py            # TM7 quality rule strategy set
├── annotation_models.py  # Guided-cooking annotation DTOs (discriminated union)
├── annotations.py        # Annotation inferrer (text patterns → StepAnnotation)
├── web_import.py         # recipe-scrapers adapter → CustomRecipeDraft
├── server.py        # FastMCP instance + lifespan
└── tools/           # Thin tool adapters: one module per domain
```

**Key invariants** — do not break these without discussion:

- `session.py` is the **only** module that imports from `cookidoo_api`.
  Tools always go through the `CookidooSessionProtocol` interface.
- Tool modules in `tools/` are **thin adapters**. Business logic lives in
  `session.py`, `quality.py`, or `web_import.py`. A tool function should
  read like: validate → call session → return DTO.
- Pydantic DTOs validate at the system boundary. Domain code works with
  validated objects — no double-checks deeper in the stack.
- `session.py` uses one lock (`_login_lock`) plus a latched `_closed` flag.
  Login, re-login, and close all serialize on the same lock; the flag stays
  set after `aclose` so a stale tool call fails fast instead of silently
  bootstrapping a fresh session. `_relogin` reuses `_login_lock` instead of
  having a dedicated refresh lock.
- Auth flow: the cookie-based OAuth2 login (cookidoo-api ≥ 0.17.1) means
  there is no `auth_data` / `refresh_token()` to manage. Session cookies in
  the shared `aiohttp.ClientSession` carry the identity; on a 401 we
  re-run `client.login()` via `_relogin` instead of refreshing a token.
- The session-generation counter (`_session_generation`, exposed via the
  `session_generation` property) is the single source of truth for re-login
  races. Snapshot it **before** the request, pass the snapshot to `_relogin`
  so parallel callers do not redundantly log in.
- The HTTP session must be built with `aiohttp.CookieJar(unsafe=True)` —
  the OAuth2 redirect chain crosses domains (`cookidoo.<tld>` → CIAM →
  login-srv), and the default jar drops those cookies.

## Security considerations

- The Cookidoo password is stored as `pydantic.SecretStr` and never logged.
- The login banner logs a **redacted email** (`a***@example.com`), never the
  full address.
- All upstream error bodies pass through `_redact_error_body` before being
  surfaced. The patterns scrub `access_token`/`refresh_token`/`id_token`/
  `api_key`/`session_id`/`csrf`/`authorization`/`bearer` keys, naked
  `Bearer xxx` headers, JWTs (`eyJ…`), and email addresses; the body is also
  truncated to 200 characters.
- `_localization_origin` rejects any URL scheme other than `http`/`https`
  to prevent reflected `javascript:` or `file:` schemes from upstream.
- Every HTTP request enforces a per-request `ClientTimeout` (30 s by
  default, see `constants.HTTP_TIMEOUT_SECONDS`). Do not bypass this.
- `aiohttp.ClientSession` cleanup is reentrant and lock-protected; do not
  null `self._http` outside of `aclose()`.

## Dev environment tips

- `./run.sh` is the canonical entry point. It is idempotent, detects
  Python 3.12+, creates `.venv/`, installs the package, sources `.env`,
  validates credentials, and `exec`s the server.
- For HTTP transport: `COOKIDOO_MCP_MODE=http ./run.sh`.
- For local development without the script:
  `source .venv/bin/activate && cookidoo-mcp`.
- The MCP Inspector is the fastest way to smoke-test tool changes:

  ```bash
  npx @modelcontextprotocol/inspector ./run.sh
  ```

- Stdio MCP servers must keep `stdout` clean — only the MCP wire protocol
  goes there. All logs go to `stderr` (the `logging.basicConfig` call in
  `__main__.py` enforces that, and `aiohttp`/`cookidoo_api` loggers are
  pinned to `WARNING`).

## Pull request expectations

- Open a PR only when `./check.sh` is fully green locally.
- Update `README.md` and this file if you change build commands, env vars,
  or registered tool names.
- Keep tool names stable — they are part of the public MCP contract.
