# AGENTS.md

Instructions for AI coding agents working in this repository. Agent-agnostic
by design — Claude Code, Cursor, Aider, OpenAI Codex CLI, and any other tool
that consumes [AGENTS.md](https://agents.md/) should follow what is described
here.

## Project overview

`cookidough-mcp` is a Model Context Protocol (MCP) server that exposes the
Thermomix [Cookidoo](https://cookidoo.de) platform to LLM clients (Claude
Desktop, Claude Code, any MCP-aware tool). It consolidates four predecessor
projects into a single Python 3.12+ codebase (tested through 3.14) built
on the **MCP Python SDK** (`MCPServer`, mcp 2.x) and the
[`miaucl/cookidoo-api`](https://github.com/miaucl/cookidoo-api) library.

The full feature list and tool table is in [`README.md`](README.md).

## Setup commands

```bash
cp .env.example .env  # fill in COOKIDOUGH_EMAIL / COOKIDOUGH_PASSWORD
./run.sh              # idempotent: bootstrap venv + install + start server
```

`run.sh` skips the install step when `pyproject.toml` has not changed since
the last successful install, so subsequent runs start immediately. It parses
no options of its own — configuration is entirely environment-driven.

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
  introspection by Pydantic/MCPServer).
- **Comments**: Default to none. Only add a comment when the **why** is
  non-obvious (a hidden constraint, a workaround for an upstream quirk).
  Never document what the code does — sprechende identifiers cover that.
- **Docstrings**: Only on public modules, classes, and tool functions. Tool
  docstrings are surfaced to the LLM client, so keep them short, precise, and
  outcome-focused.
- **Imports**: Inside `src/cookidough_mcp/tools/*` relative imports from the
  parent package (`from ..context import ...`) are intentional and ignored
  by ruff's TID rule via per-file config.

## Testing instructions

- Async tests are the default (`asyncio_mode = "auto"` in `pyproject.toml`).
- `tests/conftest.py` provides a `FakeSession` that is statically guarded
  against `CookidoughSessionProtocol`:

  ```python
  _PROTOCOL_GUARD: CookidoughSessionProtocol = FakeSession()
  ```

  If you add a method to the protocol, the guard breaks at mypy time —
  update the fake in the same PR.
- Test the **behaviour**, not the implementation. Avoid tests that assert
  "mock was called" without verifying observable output.
- Private MCPServer API access is centralized in `tests/_mcp_internals.py`.
  If you need a tool function inside a test, route through there.
- New session methods need both a unit test (DTO mapping, error paths) and
  an integration-style test via `tests/test_session_methods.py`.

## Architecture

```
src/cookidough_mcp/
├── config.py        # Pydantic-settings, env-driven Settings
├── constants.py     # Magic numbers / strings (timeouts, defaults)
├── context.py       # AppContext dataclass + ToolContext type alias
├── errors.py        # Domain exception hierarchy
├── models.py        # Pydantic DTOs for every tool I/O
├── session.py       # Repository facade over cookidoo-api + custom HTTP
├── china_client.py  # Cookidoo client for the mainland-China deployment
├── transport.py     # Stdio / HTTP transport strategies
├── quality.py            # Thermomix recipe quality rule strategy set
├── annotation_models.py  # Guided-cooking annotation DTOs (discriminated union)
├── annotations.py        # Annotation inferrer (text patterns → StepAnnotation)
├── web_import.py         # recipe-scrapers adapter → CustomRecipeDraft
├── resources.py          # MCP resources + prompts (read-only context, workflows)
├── server.py        # MCPServer instance + lifespan
└── tools/           # Thin tool adapters: one module per domain
```

**Key invariants** — do not break these without discussion:

- `session.py` and `china_client.py` are the **only** modules that import from
  `cookidoo_api`. Tools always go through the `CookidoughSessionProtocol`
  interface. `china_client.py` is exempt because it subclasses the upstream
  client; keep every other module free of that import.
- `ChinaCookidoo` overrides only the four CIAM-specific steps (discovery,
  origin guard, login form, credential POST) and reuses the base class for
  PKCE, the code exchange and token storage. It must end a login with real
  tokens: `_ensure_token` gates all 42 `cookidoo-api` methods, so a
  cookie-only login would break every one of them.
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
- Auth flow: `login()` (cookidoo-api ≥ 0.18) performs an OAuth2
  authorization-code + PKCE exchange and authenticates every later call
  with a `Bearer` token. The library refreshes an expired access token on
  its own via the stored refresh token; on a 401 we re-run `client.login()`
  through `_relogin` rather than driving `refresh()` ourselves. The login
  redirects still need the cookie jar, so `CookieJar(unsafe=True)` stays
  required.
- Two auth mechanisms coexist. `cookidoo-api` calls carry the `Bearer`
  token; the undocumented `cookidoo.<tld>` endpoints in `_authed_http` are
  authenticated by the cookie jar that `login()` fills, and reject an
  `Authorization` header. `load_token` restores only the token, so after a
  restart from `COOKIDOUGH_TOKEN_FILE` the `_authed_http` paths 401 once
  and recover through `_relogin`. Persistence therefore saves a login only
  for the `cookidoo-api` paths.
- The session-generation counter (`_session_generation`, exposed via the
  `session_generation` property) is the single source of truth for re-login
  races. Snapshot it **before** the request, pass the snapshot to `_relogin`
  so parallel callers do not redundantly log in.
- The HTTP session must be built with `aiohttp.CookieJar(unsafe=True)` —
  the OAuth2 redirect chain crosses domains (`cookidoo.<tld>` → CIAM →
  login-srv), and the default jar drops those cookies.
- The interaction endpoints (rating, bookmark, recipe-notes,
  cooking-history, recommender, customer-devices) are **undocumented**
  Cookidoo APIs discovered via the platform's `.well-known/home` document.
  Methods and payload shapes were verified live on 2026-06-05 (see
  `tests/smoke/smoke_test.py`); parsers in `session.py` must still
  tolerate missing fields and degrade to `None`/empty instead of raising,
  and per-action failures in `set_recipe_interactions` are reported in the
  result DTO rather than failing the call.
- Naming (legal requirement): the **project** is "cookidough" /
  "Cookidough" — never "cookidoo". This covers every project-owned
  identifier: the server name, resource URIs, env prefix, config keys,
  and class prefixes (`CookidoughSession`, `CookidoughSessionProtocol`,
  `CookidoughMcpError`). "Cookidoo" may appear only when referring to the
  third-party platform itself (prose, platform URLs) or in identifiers
  imported from the upstream `cookidoo_api` package, which we cannot
  rename.

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
- The optional token file (`COOKIDOUGH_TOKEN_FILE`) holds the OAuth2
  access and refresh tokens — equivalent to a password, and longer-lived
  than a session cookie. `_persist_token` creates it with `0600` *before*
  writing, since `save_token` writes in place; never log its contents,
  never widen its permissions, and keep the `.gitignore` patterns
  (`token.json`, `*.token.json`) intact.
- `set_custom_recipe_image` uploads the image bytes directly to Vorwerk's
  Cloudinary tenant (third-party egress, same as the official web app).
  The upload runs on a dedicated plain `aiohttp.ClientSession` — the
  Cookidoo cookie jar must NEVER be sent to that host. URL sources are
  restricted to http/https and capped at `MAX_RECIPE_IMAGE_BYTES`.

## Dev environment tips

- `./run.sh` is the canonical entry point. It is idempotent, detects
  Python 3.12+, creates `.venv/`, installs the package, sources `.env`,
  validates credentials, and `exec`s the server.
- For HTTP transport: `COOKIDOUGH_MCP_MODE=http ./run.sh`.
- For local development without the script:
  `source .venv/bin/activate && cookidough-mcp`.
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
