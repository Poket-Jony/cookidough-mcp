# Cookidoo MCP Server

[![CI](https://github.com/Poket-Jony/cookidoo-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Poket-Jony/cookidoo-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-FastMCP-8A2BE2.svg)](https://modelcontextprotocol.io)

An unofficial [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for the
Thermomix® [Cookidoo®](https://cookidoo.de) platform. Plug it into Claude Desktop,
Claude Code or any other MCP-aware client and let your LLM search
recipes, manage shopping lists and meal plans, and upload custom recipes.

> **Unofficial project.** This is an independent, community-built MCP server.
> It is **not** developed, sponsored, endorsed, or affiliated with Vorwerk,
> Thermomix®, or Cookidoo®. The name "Thermomix" and "Cookidoo" is used here
> purely to identify the third-party service this software talks to. See
> [Disclaimer & trademarks](#disclaimer--trademarks) for details.

- 37 MCP tools across 6 domains (auth, recipes, collections, shopping,
  calendar, discovery)
- Dual transport: stdio (default) and streamable HTTP
- Thermomix quality gate that blocks low-quality custom recipe uploads
- Guided-cooking annotations (TTS time/speed spans, INGREDIENT spans) —
  delivered explicitly by the LLM or inferred server-side from plain text
- Web recipe import via [`recipe-scrapers`](https://github.com/hhursev/recipe-scrapers)
  (200+ supported sites)
- Keyword search of the Cookidoo recipe library
- Ingredient-based recipe suggestions over the user's own collections

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [MCP client setup](#mcp-client-setup)
- [Configuration](#configuration)
- [Tool reference](#tool-reference)
- [Quality gate](#quality-gate)
- [Guided-cooking annotations](#guided-cooking-annotations)
- [HTTP transport](#http-transport)
- [Development](#development)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)
- [Disclaimer & trademarks](#disclaimer--trademarks)
- [License](#license)

## Features

A high-level overview of what the server can do, grouped by domain. The
exact tool names live in the [Tool reference](#tool-reference) further
below.

### Account & authentication

- Lazy login on first tool call — no separate connect step.
- Read the user profile and active subscription, including subscription
  level, type, and expiry.

### Recipe lookup, creation & import

- Fetch full Cookidoo recipe details (`get_recipe_details`).
- List, read, delete the authenticated user's custom recipes.
- Clone any Cookidoo recipe into the user's custom recipes at a chosen
  serving size (`clone_recipe_as_custom`).
- Build, validate, and upload custom Thermomix recipes from structured input,
  with guided-cooking annotations (`TTS`, `INGREDIENT`, all seven `MODE`
  variants).
- Import a recipe directly from any of 200+ supported recipe sites via
  `recipe-scrapers`, returning the parsed draft + a quality report and
  uploading when the gate passes.
- Keyword **search** of the Cookidoo recipe library
  (`search_recipes`), localized to the configured country/language.
- Ingredient-based **suggestions** over the user's managed and custom
  collections (`suggest_recipes_from_ingredients`).

### Collections (managed + custom)

- Browse and subscribe to / unsubscribe from Cookidoo-curated managed
  collections.
- Full CRUD over custom collections: create, list, delete, add recipes,
  remove a single recipe.

### Shopping list

- Read the full list grouped by source (recipe ingredients vs. free-text
  items).
- Push and pull recipe ingredients for regular **and** custom recipes
  (`add_recipes_to_shopping_list`, `add_custom_recipes_to_shopping_list`,
  the matching `remove_*` variants).
- Add, remove, rename, and check / uncheck free-text shopping items
  (`add_additional_items`, `rename_additional_items`,
  `set_additional_items_ownership`).
- Check / uncheck recipe-derived ingredient items
  (`set_ingredient_items_ownership`).
- Wipe the whole list in one call.

### Calendar / meal plan

- Read the meal plan for any week.
- Schedule and remove regular **and** custom recipes on a specific date
  (`add_recipes_to_calendar`, `add_custom_recipes_to_calendar`,
  `remove_recipe_from_calendar`, `remove_custom_recipe_from_calendar`).

### Quality, safety & transport

- Thermomix [quality gate](#quality-gate) that scores every draft and
  refuses low-quality custom recipe uploads unless explicitly forced.
- All credentials kept in memory as `SecretStr`, with email + token
  redaction in every log message and upstream error body.
- Per-request HTTP timeout; reentrant, lock-protected session lifecycle.
- Dual transport: stdio (default) and streamable HTTP.

## Requirements

- Python **3.12 or newer**
- A valid Cookidoo account (`COOKIDOO_EMAIL` / `COOKIDOO_PASSWORD`)
- Optional: [`uv`](https://docs.astral.sh/uv/) for the recommended client
  setup, or `pip` if you prefer

## Quickstart

```bash
git clone https://github.com/jonasplamann/cookidoo-mcp.git
cd cookidoo-mcp
cp .env.example .env          # fill in COOKIDOO_EMAIL / COOKIDOO_PASSWORD
./run.sh
```

`run.sh` is idempotent: it detects Python 3.12+, creates `.venv/`, installs
the project the first time around, loads `.env`, validates credentials, and
starts the server. Subsequent runs skip the install step and start
immediately. Any extra arguments are forwarded to `cookidoo-mcp`.

```bash
./run.sh --help                       # CLI help
COOKIDOO_MCP_MODE=http ./run.sh       # start over HTTP instead of stdio
```

## MCP client setup

### Claude Desktop / Claude Code

Two ways to wire it up — pick one and add it to your MCP client config
(`claude_desktop_config.json` or `~/.claude/mcp.json`).

**Option A — using `run.sh` (recommended, no extra tooling):**

```json
{
  "mcpServers": {
    "cookidoo": {
      "command": "/absolute/path/to/cookidoo-mcp/run.sh",
      "env": {
        "COOKIDOO_EMAIL": "you@example.com",
        "COOKIDOO_PASSWORD": "..."
      }
    }
  }
}
```

**Option B — using `uvx` (no clone required; works
locally with `--from`):**

```json
{
  "mcpServers": {
    "cookidoo": {
      "command": "uvx",
      "args": [
        "--from",
        "/absolute/path/to/cookidoo-mcp",
        "cookidoo-mcp"
      ],
      "env": {
        "COOKIDOO_EMAIL": "you@example.com",
        "COOKIDOO_PASSWORD": "...",
        "COOKIDOO_COUNTRY": "de",
        "COOKIDOO_LANGUAGE": "de-DE",
        "COOKIDOO_QUALITY_BAR": "70"
      }
    }
  }
}
```

### Smoke-test with the MCP Inspector

```bash
npx @modelcontextprotocol/inspector ./run.sh
```

The inspector lists every registered tool and lets you call them
interactively.

## Configuration

The server is configured purely via environment variables (see
[`.env.example`](.env.example)):

| Variable                | Required | Default     | Description                                              |
| ----------------------- | -------- | ----------- | -------------------------------------------------------- |
| `COOKIDOO_EMAIL`        | yes      | -           | Cookidoo account email                                   |
| `COOKIDOO_PASSWORD`     | yes      | -           | Cookidoo account password (stored in memory as `SecretStr`) |
| `COOKIDOO_COUNTRY`      | no       | `de`        | ISO 3166-1 alpha-2 country code (case-insensitive)       |
| `COOKIDOO_LANGUAGE`     | no       | `de`        | ISO 639-1 (`de`, paired with `COOKIDOO_COUNTRY`) or BCP-47 (`de-DE`); case-normalized to `lang-REGION` |
| `COOKIDOO_MCP_MODE`     | no       | `stdio`     | Transport: `stdio` or `http`                             |
| `COOKIDOO_MCP_HOST`     | no       | `127.0.0.1` | Bind host (HTTP only)                                    |
| `COOKIDOO_MCP_PORT`     | no       | `8765`      | Bind port (HTTP only)                                    |
| `COOKIDOO_QUALITY_BAR`  | no       | `70`        | Minimum Thermomix recipe quality score (0-100) for custom uploads |

## Tool reference

All tools are registered automatically on server start. Each tool returns
a strongly typed Pydantic DTO (see [`src/cookidoo_mcp/models.py`](src/cookidoo_mcp/models.py)).

### Authentication & account

| Tool                 | Purpose                                                |
| -------------------- | ------------------------------------------------------ |
| `get_user_profile`   | Return the authenticated user's Cookidoo profile (also triggers the lazy login on first use) |
| `get_subscription`   | Return the active Cookidoo subscription, if any        |

### Recipes

Lookup of any Cookidoo recipe plus the full custom-recipe workflow
(generate → validate → upload, list / delete, scrape from supported sites).

| Tool                        | Purpose                                                                                       |
| --------------------------- | --------------------------------------------------------------------------------------------- |
| `get_recipe_details`        | Full details of a Cookidoo recipe by ID                                                       |
| `get_custom_recipe_details` | Full details of one of your own custom recipes by ID                                          |
| `generate_recipe_structure` | Build a validated custom-recipe draft (steps accept plain strings or structured `RecipeStep`s — see [Guided-cooking annotations](#guided-cooking-annotations)) |
| `validate_recipe_quality`   | Score a draft against the Thermomix recipe quality bar without uploading                      |
| `upload_custom_recipe`      | Upload a draft (rolls back on failure, blocked by [Quality gate](#quality-gate))              |
| `list_custom_recipes`       | List all custom recipes you own                                                               |
| `delete_custom_recipe`      | Delete one of your custom recipes by ID                                                       |
| `clone_recipe_as_custom`    | Copy a Cookidoo recipe into your custom recipes at a chosen serving size                      |
| `import_web_recipe`         | Scrape a recipe; always returns the draft + quality report, uploads only when the gate passes |

Custom recipe upload talks to the same undocumented `/created-recipes/{locale}`
endpoint that the official Cookidoo apps use.

### Collections

| Tool                                  | Purpose                                              |
| ------------------------------------- | ---------------------------------------------------- |
| `list_managed_collections`            | List Cookidoo-curated collections you subscribe to   |
| `add_managed_collection`              | Subscribe to a managed collection by ID              |
| `remove_managed_collection`           | Unsubscribe from a managed collection                |
| `list_custom_collections`             | List your own custom collections                     |
| `create_custom_collection`            | Create a new empty custom collection                 |
| `delete_custom_collection`            | Delete a custom collection (recipes are kept)        |
| `add_recipes_to_custom_collection`    | Add one or more recipes to a custom collection       |
| `remove_recipe_from_custom_collection` | Remove a single recipe from a custom collection      |

### Shopping list

| Tool                                       | Purpose                                                            |
| ------------------------------------------ | ------------------------------------------------------------------ |
| `get_shopping_list`                        | Return all items grouped by source (recipe / additional)           |
| `add_recipes_to_shopping_list`             | Add all ingredients of one or more recipes                         |
| `remove_recipes_from_shopping_list`        | Remove ingredients of given recipes                                |
| `add_custom_recipes_to_shopping_list`      | Add all ingredients of one or more **custom** recipes              |
| `remove_custom_recipes_from_shopping_list` | Remove ingredients of given **custom** recipes                     |
| `set_ingredient_items_ownership`           | Check or uncheck recipe-derived ingredient items by ID             |
| `add_additional_items`                     | Add free-text items (not tied to a recipe)                         |
| `rename_additional_items`                  | Rename free-text items in place by ID                              |
| `set_additional_items_ownership`           | Check or uncheck free-text items by ID                             |
| `remove_additional_items`                  | Remove free-text items by ID                                       |
| `clear_shopping_list`                      | Remove every item from the list                                    |

### Calendar / meal plan

| Tool                              | Purpose                                                  |
| --------------------------------- | -------------------------------------------------------- |
| `get_calendar_week`               | Meal plan for the week containing the given date         |
| `add_recipes_to_calendar`         | Schedule one or more recipes on a specific date          |
| `remove_recipe_from_calendar`     | Remove a planned recipe from a date                      |
| `add_custom_recipes_to_calendar`  | Schedule one or more **custom** recipes on a date        |
| `remove_custom_recipe_from_calendar` | Remove a planned **custom** recipe from a date        |

### Discovery (search & suggestions)

| Tool                                | Purpose                                                        |
| ----------------------------------- | -------------------------------------------------------------- |
| `search_recipes`                    | Keyword search of the Cookidoo recipe library                  |
| `suggest_recipes_from_ingredients`  | Rank recipes in the user's collections by ingredient match     |

## Quality gate

Both `upload_custom_recipe` and `import_web_recipe` score every draft against
a small ruleset (time/speed annotations per cooking step, temperature/Varoma
mode, accessory mentions, parallelization hints, ingredient/step linkage).

- Default threshold: `COOKIDOO_QUALITY_BAR=70`
- `upload_custom_recipe` raises `QualityGateError` when below threshold —
  the LLM submitted the draft itself, a hard error is the right signal.
- `import_web_recipe` **never** raises on quality; it always returns a
  `WebImportResult` with `draft` + `quality` populated and `upload=null`
  + `blocked_reason` set when blocked. The caller (typically an LLM) can
  read the scraped draft, rewrite the steps with Thermomix guided-cooking
  annotations (e.g. `5 min / 100 °C / speed 3`) and resubmit via
  `upload_custom_recipe` — no second scrape needed.
- Pass `force=true` on either tool to upload anyway after the user has
  explicitly accepted a sub-threshold upload
- Call `validate_recipe_quality` first to see actionable issues without
  attempting the upload

## Guided-cooking annotations

A `RecipeStep` carries both human-readable `text` and an optional list of
typed `annotations` that turn substrings into interactive guided-cooking
spans in the Cookidoo app:

All `speed` values are **strings** — numeric speeds must be quoted
(`"4"`, `"6.5"`, …), and `"soft"` is the keyword for *Sanftrührstufe*.
Pydantic rejects unquoted numbers at the boundary.

### `tools` — device compatibility, not accessories

`CustomRecipeDraft.tools` lists which Thermomix device **generations**
the recipe is compatible with. Only three tokens are accepted:

| Value   | Meaning                                                          |
| ------- | ---------------------------------------------------------------- |
| `"TM5"` | Pre-2019 device, no Sanftrührstufe, no browning/steaming/dough/warm_up/blend/turbo/rice_cooker MODE |
| `"TM6"` | Adds Sanftrührstufe (`speed="soft"`) and the browning, steaming, dough, warm_up, blend, turbo MODEs |
| `"TM7"` | Adds the rice_cooker MODE                                         |

It is **not** a list of accessories or in-bowl tools — `"Mixtopf"`,
`"Spatel"`, `"Varoma"`, `"Schmetterling"`, etc. are rejected by Pydantic
with a `literal_error`. Reference those in the step text instead.

Choose the **lowest** TM generation that can still run every step, and
list it together with all higher generations (e.g. `["TM7", "TM6"]` for a
recipe that uses `Sanftrührstufe` but no rice_cooker). Default: all
three.

### Top-level annotation types

- **`TTS`** — Thermomix time + speed instruction the app dispatches.
  - `speed`: `"<n>"` (e.g. `"4"`, `"6.5"`) or `"soft"` for Sanftrührstufe
  - `time`: integer seconds
  - `temperature`: optional `{ "value": "<°C>", "unit": "C" }`
  - `direction`: optional `"CW"` or `"CCW"` (counter-clockwise for Linkslauf)
- **`INGREDIENT`** — Highlights an ingredient reference inside the step text.
  - `description`: canonical ingredient entry — may differ from the visible
    span text (e.g. span `"1 EL Salz"`, description `"Salz und Pfeffer"`)
- **`MODE`** — Thermomix program. Carries an additional top-level lowercase
  `name` field; the `data` shape depends on the name (see below).

### MODE / `<name>` data shapes

- **`browning`** — Browning program (140-160 °C in 5 °C steps, ≤30 min):
  `{ "time": <s>, "temperature": { "value": "<°C>", "unit": "C" }, "power": "Intense"|"Gentle" }`
- **`steaming`** — Varoma steaming:
  `{ "time": <s>, "speed": "<n>"|"soft", "direction": "CW"|"CCW", "accessory": "Varoma"|"SimmeringBasket"|"VaromaAndSimmeringBasket" }`
- **`dough`** — Kneading: `{ "time": <s> }`
- **`turbo`** — Pulse mode (sub-second `time` supported):
  `{ "time": <s|float>, "pulseCount": <n> }`
- **`rice_cooker`** — Empty `{}` (no parameters)
- **`warm_up`** — Warm-up (time is optional):
  `{ "speed": "<n>"|"soft", "temperature": { "value": "<°C>", "unit": "C" }, "time"?: <s> }`
- **`blend`** — Blending: `{ "speed": "<n>", "time": <s> }`

Each annotation pins its span via `offset` and `length`, counted in
Python `str` units over the step `text` (Unicode code points; this
matches what Cookidoo accepted in our PATCH captures, including
umlauts). Two ways to populate them:

1. **Explicit (LLM-supplied)** — when `generate_recipe_structure` or
   `upload_custom_recipe` receives a `RecipeStep` with `annotations`, the
   spans go to Cookidoo unchanged. This is the most precise route and is
   what the LLM should prefer when it knows the exact substrings.

   ```json
   {
     "text": "200 g Mehl in den Mixtopf geben, 30 Sek. / Stufe 4 verkneten.",
     "annotations": [
       { "type": "INGREDIENT",
         "data": { "description": "200 g Mehl" },
         "offset": 0, "length": 10 },
       { "type": "TTS",
         "data": { "speed": "4", "time": 30 },
         "offset": 33, "length": 17 }
     ]
   }
   ```

2. **Inferred (server-side)** — when a step is passed as a plain string,
   or with an omitted or empty `annotations` list (the two are
   equivalent), the server scans the text on upload:

   - `<n> Min./<temp> °C/(Leicht|Intensiv|Gentle|Intense)` becomes a
     `MODE/browning` span (temperatures outside the Cookidoo whitelist
     of 140-160 °C in 5 °C steps and durations above 30 min are dropped).
   - `<time>/Varoma/Stufe <n>` becomes a `MODE/steaming` span.
   - `<n> Min./Teigstufe` (and `Stufe Teig` / `dough mode` /
     `knead mode`) becomes a `MODE/dough` span.
   - `<time>[/<°C>][/Linkslauf]/Stufe <n>[/Linkslauf]` becomes a `TTS`
     span; the temperature segment is captured when present, and the
     reverse-blade token may appear *either* before or after the speed
     (both orderings are common in real recipes). The reverse-blade
     token also matches `reverse`, `counterclockwise`, `anticlockwise`
     and `sens inverse`.
   - `<time>[/<°C>][/Linkslauf]/Sanftrührstufe` (or `Stufe sanft` /
     `speed soft`) becomes a `TTS` span with `speed="soft"`.
   - `<time>[/<°C>]/Anbratstufe[/Linkslauf]` (TM7 browning-mode stir
     pattern; the `Bratstufe` shorthand is also accepted) becomes a
     `TTS` span with `speed="anbrat"`.
   - Ingredient-list entries are reduced to their head noun: quantity,
     unit and parenthetical hints are stripped. Quantities recognised
     include integers (`350`), decimals (`1,5`, `1.5`), ASCII fractions
     (`1/4`), Unicode vulgar fractions (`½`, `¼`, `⅔`, ...), mixed
     fractions (`1 ½`), and ranges (`2-3`). Units cover the common
     vocabulary across Cookidoo's main locales (DE, EN, FR, IT, ES, NL) —
     including `g`/`ml`/`EL`/`TL`/`Prise`/`Bund`/`Stiel(e)`/`Stängel`/
     `Blätter`/`Pck.`/`Tasse`/`Klacks`/`Block`/`Kugel`/`Rispe`/`Strauß`,
     `tbsp`/`cup`/`pinch`/`clove(s)`/`head(s)`/`leaves`, French
     `cuillère`/`pincée`/`gousse(s)`/`boîte`, Italian `cucchiaio`/`spicchio`/
     `pizzico`/`foglia`, Spanish `cucharada`/`diente(s)`/`pizca`/`manojo`,
     Dutch `eetlepel`/`snufje`/`teen`/`takje` — so `"3 Stängel Petersilie"`
     → head `"Petersilie"`, `"½ TL Zucker"` → head `"Zucker"`,
     `"1 cuillère de farine"` → head `"farine"`, `"2 spicchi di aglio"`
     → head `"aglio"`. Romance/English linker words (`de`/`di`/`du`/`of`/
     `van`/...) right after the unit are also stripped. Trailing comma
     descriptors are dropped (`"100 ml Weißwein, trocken"` → `"Weißwein"`).
   - The head is matched in the step text with a length-gated pattern:
     - **≥ 5 chars**: compound-prefix tolerant. `Petersilie` matches
       inside `Petersilienblättchen`; `Zwiebel` matches `Zwiebeln`. The
       annotated span covers the **whole** compound.
     - **3–4 chars**: only known German inflection endings are accepted
       (`Salz` → `Salzen`). Compound matching is off to avoid
       `Reis` → `Reisebus` false positives.
     - **≤ 2 chars**: exact match only. Very short heads (`Ei`, `Öl`)
       would otherwise collide with function words (`Ein`, `ein`).
   - The leading word boundary is always strict, so a head never matches
     as the **suffix** of a compound — `Öl` does not match inside
     `Olivenöl` (different ingredient).
   - The annotated span also pulls in any **quantity (and optional unit)**
     that immediately precedes the head in the step text — so
     `"20 g Haselnüsse in den Mixtopf geben"` is annotated as
     `"20 g Haselnüsse"`, `"1 Prise Salz zugeben"` as `"1 Prise Salz"`,
     and `"1 Ei verquirlen"` as `"1 Ei"`. When the measurement is not
     repeated in the step text, only the noun is highlighted.
   - When the primary head itself ends with a known **portion word**
     (`Spargelstücke` = `Spargel` + `stücke`, `Ziegenkäsescheiben` =
     `Ziegenkäse` + `scheiben`), the inferrer also matches the
     shorter form — so the same ingredient line is found both at
     `"500 g Spargelstücke"` and at a later standalone `"Spargel"`.
   - When the head is **multiple whitespace-separated words** and the
     last word is itself long enough for compound matching (≥ 5 chars),
     that last word is added as a secondary head — so `"500 g weißer
     Spargel"` (adj + noun) still matches step text that uses a declined
     `"weißen Spargel"` or just `"Spargel"`. The adjective itself is
     not recovered into the span.
   - `description` carries the **full canonical ingredient line** (not
     the matched substring) so Cookidoo can resolve quantities.
   - Not handled (supply explicit annotations for these): reverse plural
     (plural ingredient line → singular step), arbitrary compound
     splits without a known portion-word suffix (ingredient
     `Ziegenfrischkäse` ≠ step `Ziegenkäse-Scheiben`, ingredient
     `Gemüsemaultaschen` ≠ step `Maultaschen` — the *middle* of a
     compound cannot be inferred), and umlaut plurals (`Apfel` →
     `Äpfel`).

   To suppress inference, pass at least one explicit annotation. The
   remaining MODE types (`turbo`, `rice_cooker`, `warm_up`, `blend`)
   currently have no text-pattern detector — the LLM must supply them
   explicitly.

## HTTP transport

For remote clients or web-based MCP integrations:

```bash
COOKIDOO_MCP_MODE=http \
COOKIDOO_MCP_HOST=0.0.0.0 \
COOKIDOO_MCP_PORT=8765 \
./run.sh
```

The server then speaks the MCP streamable-HTTP protocol on the configured
host/port.

## Development

Manual environment (without `run.sh`):

```bash
git clone https://github.com/jonasplamann/cookidoo-mcp.git
cd cookidoo-mcp
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"      # or: pip install -e ".[dev]"
```

### Testing & quality gates

The canonical way to run the full suite is the bundled `check.sh` script
— it executes the four gates in order, stops at the first failure, and
prints a single "All gates passed." summary at the end:

```bash
./check.sh           # run lint + format-check + mypy + pytest
./check.sh --fix     # auto-fix ruff lint + format issues, then re-run gates
./check.sh --help    # show usage
```

Equivalent manual invocation (useful when iterating on a single gate):

```bash
ruff check .                    # lint
ruff format --check .           # formatting
mypy                            # strict type checks over src/ and tests/
pytest                          # full test suite, coverage gate ≥ 80 %
```

Targeted test runs:

```bash
pytest tests/test_annotations.py           # one file
pytest -k ingredient                       # by keyword
pytest --no-cov -x tests/test_session.py   # fast iteration without coverage
```

`./check.sh` is also the pre-PR contract: a green run is required before
opening a pull request. Coverage failures (< 80 %) and any lint/type
error block the build.

Project conventions (architecture, invariants, security model) are
documented in [`AGENTS.md`](AGENTS.md) — read it before contributing.

## Architecture

```
src/cookidoo_mcp/
├── config.py        # Pydantic-settings, env-driven Settings
├── constants.py     # Timeouts and other shared constants
├── context.py       # AppContext + ToolContext type alias
├── errors.py        # Domain exception hierarchy
├── models.py            # Pydantic DTOs for every tool I/O
├── annotation_models.py # Guided-cooking annotation DTOs (discriminated union)
├── session.py           # Repository facade over cookidoo-api + custom HTTP
├── transport.py         # Stdio / HTTP transport strategies
├── quality.py           # Thermomix recipe quality rule strategies
├── annotations.py       # Annotation inferrer (text patterns → StepAnnotation)
├── web_import.py        # recipe-scrapers adapter → CustomRecipeDraft
├── server.py        # FastMCP instance + lifespan
└── tools/           # Thin tool adapters: one module per domain
```

`session.py` is the only module that imports from `cookidoo-api`; every
tool talks to the session through `CookidooSessionProtocol`, so swapping
the upstream client only touches one file.

## Troubleshooting

**`Missing required environment variable(s): COOKIDOO_EMAIL`**
Copy `.env.example` to `.env` and fill in your credentials, or set the
variables in your MCP client config.

**`Python 3.12 or newer is required but was not found on PATH`**
Install Python 3.12+ (macOS: `brew install python@3.12`; Debian/Ubuntu:
`apt install python3.12`) and re-run `./run.sh`.

**HTTP port already in use**
Set a different port: `COOKIDOO_MCP_PORT=9000 ./run.sh`.

**Custom recipe upload blocked by quality gate**
Either improve the draft (add Thermomix guided-cooking annotations such as
`5 min / 100 °C / speed 3` to each step), lower `COOKIDOO_QUALITY_BAR`, or
re-issue the call with `force=true` after the user accepts the trade-off.

**Stdio client sees corrupted JSON-RPC frames**
This server keeps `stdout` clean for MCP traffic — only the wire protocol
goes there, all logs go to `stderr`. If you wrap `run.sh` in another script,
make sure that wrapper does not write to stdout either.

**`Access token request failed due to bad request, please check your email or refresh token`**
Vorwerk retired the `grant_type=password` OAuth flow used by
`cookidoo-api ≤ 0.17.0` in May 2026. This project requires `cookidoo-api
≥ 0.17.1`, which ships the browser OAuth2 cookie flow. If you see this
error you're on an older version — run `./run.sh` (the install marker is
keyed off `pyproject.toml`, so editing it forces a reinstall) or, for a
manual install, `pip install --upgrade 'cookidoo-api>=0.17.1'`.

## Credits

Built on top of the unofficial API client and informed by the
earlier community MCP servers in this space. Thanks to:

- [`miaucl/cookidoo-api`](https://github.com/miaucl/cookidoo-api)
- [`alexandrepa/mcp-cookidoo`](https://github.com/alexandrepa/mcp-cookidoo)
- [`Xdev22/cookidoo-mcp`](https://github.com/Xdev22/cookidoo-mcp)
- [`detef10/cookidoo-mcp`](https://github.com/detef10/cookidoo-mcp)
- [`danielkliem/mcp-cookidoo`](https://github.com/danielkliem/mcp-cookidoo)
- [`otisthescribe/cookidoo-mcp`](https://github.com/otisthescribe/cookidoo-mcp)

## Disclaimer & trademarks

This is an **independent, unofficial** project maintained by community
contributors. It is not developed, sponsored, endorsed, authorised, or in
any way affiliated with Vorwerk SE & Co. KG, Vorwerk International AG,
Thermomix®, Cookidoo®, or any of their subsidiaries.

**Cookidoo®** and **Thermomix®** are registered trademarks of Vorwerk
International AG. **TM5**, **TM6**, **TM7** and related model designations
are likewise Vorwerk-owned marks. All other product names, logos and
brands referenced in this repository are the property of their respective
owners.

**Account & terms:** Operating this server requires your own credentials.
Your use of the service through this server remains subject to Vorwerk's official
[Terms of Use](https://cookidoo.de/consent/web/documents/de-DE/latest/tos) and
[Privacy Policy](https://cookidoo.de/consent/web/customers/de-DE/documents/PRIVACY). This
project does not redistribute Cookidoo® content; recipes you fetch are
delivered directly from Vorwerk's servers to your client.

**Warranty:** The software is provided "as is" without warranty of any
kind. See [License](#license).

## License

[MIT](LICENSE)
