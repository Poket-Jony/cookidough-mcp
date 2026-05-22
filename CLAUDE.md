@AGENTS.md

## Claude Code

The shared `AGENTS.md` above is the source of truth for project conventions,
build commands, code style, testing, architecture, and security. The notes
below only cover behaviours that are specific to Claude Code.

- **Plan mode for cross-cutting changes**: Use plan mode whenever a change
  touches `session.py`, the locking model, the session-generation counter,
  or the FastMCP lifespan. These areas have layered invariants (see the
  "Architecture" section in `AGENTS.md`) that are easy to break in an
  unplanned edit.
- **Use the `Explore` subagent before non-trivial edits** to confirm which
  modules a change crosses — this project keeps `cookidoo_api` isolated to
  `session.py` and that boundary is easy to violate by accident.
- **Run the gates yourself before reporting "done"**: `./check.sh` (or the
  equivalent `ruff check . && ruff format --check . && mypy && pytest`).
  A passing pytest is necessary but not sufficient — mypy and ruff catch a
  different class of regressions.
- **Avoid Claude-only files**: do not introduce `.cursorrules`,
  `.windsurfrules`, or other tool-specific instruction files. Anything that
  applies to coding agents in general belongs in `AGENTS.md`.
- **Memory hygiene**: this repository's conventions are stable and stored
  here in `AGENTS.md`. Do not duplicate them into auto-memory; prefer
  editing `AGENTS.md` so every contributor (and every agent) sees the same
  source of truth.
