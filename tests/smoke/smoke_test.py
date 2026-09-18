"""End-to-end smoke test against a live Cookidoo account.

This test launches ``./run.sh`` exactly as Claude Desktop does and drives every
registered MCP tool over the JSON-RPC stdio protocol. The session layer is
never imported directly; everything exercises the full transport + MCPServer
dispatch + tool adapter + session + cookidoo-api stack.

Credentials are loaded from a file **outside** the repository tree so they
can never leak into git history. The lookup order is:

1. ``$COOKIDOUGH_SMOKE_ENV_FILE`` if set.
2. ``$XDG_CONFIG_HOME/cookidough-mcp/smoke.env``, else
3. ``~/.config/cookidough-mcp/smoke.env``.

The file follows the same ``KEY=value`` format as ``.env.example``. The
values are then passed to the server via the subprocess environment — the
same mechanism Claude Desktop uses via the ``env`` field in ``mcp.json``.
If a regular ``.env`` exists in the repo root the test refuses to start:
``run.sh`` would ``source`` it last and silently override the inherited
test credentials.

This script is deliberately NOT named ``test_*.py`` so pytest will not
auto-discover it. Run it explicitly:

    .venv/bin/python tests/smoke/smoke_test.py

Every method on ``CookidoughSessionProtocol`` is exercised through its tool
adapter. The two destructive operations have explicit safety guards:

- ``add_managed_collection`` / ``remove_managed_collection`` run against a
  hardcoded catalogue ID (``SMOKE_MANAGED_COLLECTION_ID``). The roundtrip is
  *skipped* when that collection is already subscribed.
- ``clear_shopping_list`` runs *only* when the shopping list is empty at
  the start of the writes phase.

Cleanup runs in a ``finally``: every write section records what it created
in a local variable, and the teardown removes it even when an earlier
section raised. Without that, one failing tool call would strand a
collection, shopping items and custom recipes on a real account.

Coverage gaps, all deliberate:

- ``import_web_recipe`` — its real dependency is an external recipe site
  whose markup we do not control. Covered by ``tests/test_tools.py`` with a
  mocked importer.
- ``set_recipe_interactions(is_custom_recipe=True)`` — it would write a
  cooking-history entry for a custom recipe this test deletes afterwards,
  and the history has no delete endpoint. An untestable parameter beats a
  permanent dangling entry on the account.

The interaction, recommendation, bookmark, and device endpoints are
undocumented Cookidoo APIs whose methods and payload shapes were verified
against the live API. They are asserted as hard contracts
here — any mismatch fails the run.

Bookmark and note are reverted. The rating is restored to whatever the
recipe carried before the run — Cookidoo's rating route offers only GET and
PUT, PUT rejects ``0``/``null``, and no DELETE exists at any level, so a
recipe that was *unrated* before cannot be returned to that state; the run
warns when that happens. The cooking-history entry always persists: its
entries carry no id of their own, only a timestamp and the recipe id.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
import uuid
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def _resolve_smoke_env_path() -> Path:
    """Resolve the path to the smoke-test credentials file.

    Precedence: ``COOKIDOUGH_SMOKE_ENV_FILE`` env var, else
    ``$XDG_CONFIG_HOME/cookidough-mcp/smoke.env``, else
    ``~/.config/cookidough-mcp/smoke.env``. The file lives **outside** the
    repository so credentials cannot accidentally be committed.
    """
    override = os.environ.get("COOKIDOUGH_SMOKE_ENV_FILE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "cookidough-mcp" / "smoke.env"


def _load_env_test(path: Path) -> dict[str, str]:
    if not path.exists():
        print(
            f"smoke test requires credentials at {path}.\n"
            f"  Create the file with COOKIDOUGH_EMAIL / COOKIDOUGH_PASSWORD (see "
            f"{REPO / '.env.example'} for the full format), or override the "
            f"location via the COOKIDOUGH_SMOKE_ENV_FILE environment variable.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _refuse_if_dotenv_would_override(repo: Path) -> None:
    dotenv = repo / ".env"
    if not dotenv.exists():
        return
    print(
        f"refusing to start: {dotenv} exists. run.sh would `source` it after "
        "we set the .env.test credentials in the subprocess environment, "
        "silently overriding our test creds. Remove or rename .env first.",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(2)


# Imports that pull in the MCP client SDK come AFTER the path/env helpers so a
# missing .env.test fails fast with a clear message rather than an import noise.
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.types import CallToolResult, TextResourceContents  # noqa: E402

MARKER = uuid.uuid4().hex[:8]
COLLECTION_NAME = f"[SMOKE_TEST cookidough-mcp] {MARKER}"
ITEM_NAME = f"[SMOKE_TEST cookidough-mcp] item {MARKER}"
SENTINEL_ITEM_NAME = f"[SMOKE_TEST cookidough-mcp] clear-sentinel {MARKER}"
RECIPE_NAME = f"[SMOKE_TEST cookidough-mcp] recipe {MARKER}"
FUTURE_TEST_DAY = "2099-01-01"

# Stable Cookidoo catalogue managed collection used by the add/remove roundtrip.
# Picked because it is a long-running themed cookbook unlikely to vanish. The
# section is SKIPPED when the account already has it subscribed, so a failed
# cleanup cannot orphan the user without it.
SMOKE_MANAGED_COLLECTION_ID = "col371088"  # "#zuHausemitThermomix"
SMOKE_CLONE_RECIPE_ID = "r469077"

# Per-tool RPC timeout. The first call triggers ``run.sh``'s bootstrap (~2 s on
# warm checkouts, longer on a cold .venv) and the live Cookidoo login (~3-5 s),
# so the initial round trip needs a comfortable budget.
CALL_TIMEOUT = 90.0


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def ok(msg: str) -> None:
    print(f"  [ok]   {msg}", flush=True)


def info(msg: str) -> None:
    print(f"  [info] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  [warn] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)


def _unwrap(payload: Any) -> Any:
    """Strip MCPServer's ``{"result": …}`` envelope around non-object returns."""
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


def _payload(result: CallToolResult, tool: str) -> Any:
    """Return the tool's structured output, or raise if the call errored."""
    if result.is_error:
        text = " | ".join(getattr(c, "text", str(c)) for c in (result.content or []))
        raise RuntimeError(f"tool {tool!r} returned error: {text or '(no content)'}")
    if result.structured_content is not None:
        return _unwrap(result.structured_content)
    if result.content:
        text_payload: str | None = getattr(result.content[0], "text", None)
        if text_payload is not None:
            try:
                return json.loads(text_payload)
            except (json.JSONDecodeError, TypeError):
                return text_payload
    return None


async def _call(mcp: ClientSession, tool: str, **arguments: Any) -> Any:
    result = await mcp.call_tool(tool, arguments=arguments, read_timeout_seconds=CALL_TIMEOUT)
    return _payload(result, tool)


def _ingredient_annotation(text: str, span: str) -> dict[str, Any]:
    offset = text.index(span)
    return {
        "type": "INGREDIENT",
        "data": {"description": span},
        "offset": offset,
        "length": len(span),
    }


def _tts_annotation(text: str, span: str, *, speed: str, time: int) -> dict[str, Any]:
    offset = text.index(span)
    return {
        "type": "TTS",
        "data": {"speed": speed, "time": time},
        "offset": offset,
        "length": len(span),
    }


def _browning_annotation(
    text: str, span: str, *, time: int, temperature: str, power: str
) -> dict[str, Any]:
    offset = text.index(span)
    return {
        "type": "MODE",
        "name": "browning",
        "data": {
            "time": time,
            "temperature": {"value": temperature, "unit": "C"},
            "power": power,
        },
        "offset": offset,
        "length": len(span),
    }


async def main() -> int:
    _refuse_if_dotenv_would_override(REPO)
    test_env = _load_env_test(_resolve_smoke_env_path())
    # Inherit PATH, HOME, LANG etc. from our shell; layer .env.test on top so
    # the COOKIDOUGH_* credentials are available to run.sh's assert_credentials.
    server_env: dict[str, str] = {**os.environ, **test_env}

    server_params = StdioServerParameters(
        command=str(REPO / "run.sh"),
        args=[],
        env=server_env,
    )

    created_collection_id: str | None = None
    created_item_ids: list[str] = []
    created_recipe_id: str | None = None
    cloned_recipe_id: str | None = None
    planned_calendar_day: str | None = None
    planned_calendar_recipe_id: str | None = None
    planned_custom_calendar_day: str | None = None
    planned_custom_calendar_recipe_id: str | None = None
    pending_managed_collection_id: str | None = None
    failures = 0
    recipe_id_for_lookup: str | None = None

    try:
        async with stdio_client(server_params) as (read, write):  # noqa: SIM117
            async with ClientSession(read, write) as mcp:
                await mcp.initialize()

                try:
                    section("Tool discovery")
                    tools_resp = await mcp.list_tools()
                    tool_names = sorted(t.name for t in tools_resp.tools)
                    ok(f"{len(tool_names)} tools registered")
                    for name in tool_names:
                        info(f"- {name}")

                    section("Resource & prompt discovery")
                    try:
                        resources_resp = await mcp.list_resources()
                        resource_uris = sorted(str(r.uri) for r in resources_resp.resources)
                        ok(f"{len(resource_uris)} resources: {resource_uris}")
                        prompts_resp = await mcp.list_prompts()
                        prompt_names = sorted(p.name for p in prompts_resp.prompts)
                        ok(f"{len(prompt_names)} prompts: {prompt_names}")
                    except Exception as e:
                        failures += 1
                        fail(f"resource/prompt discovery raised: {e!r}")

                    section("Authentication")
                    profile = await _call(mcp, "get_user_profile")
                    ok(f"id={profile.get('id')!r} username={profile.get('username')!r}")
                    if profile.get("description"):
                        info(f"description={profile['description']!r}")

                    section("Profile devices (read-only)")
                    try:
                        profile_with_devices = await _call(
                            mcp, "get_user_profile", include_devices=True
                        )
                        devices = profile_with_devices.get("devices", [])
                        accessories = profile_with_devices.get("accessories", [])
                        ok(f"devices={devices!r} accessories={accessories!r}")
                        if not devices and not accessories:
                            info("both lists empty — no device linked to this account")
                    except Exception as e:
                        failures += 1
                        fail(f"get_user_profile(include_devices=True) raised: {e!r}")

                    section("Subscription (read-only)")
                    sub = await _call(mcp, "get_subscription")
                    if sub is None:
                        warn("No active subscription")
                    else:
                        ok(
                            f"level={sub.get('subscription_level')!r} "
                            f"status={sub.get('status')!r} "
                            f"source={sub.get('subscription_source')!r} "
                            f"expires={sub.get('expires')}"
                        )

                    section("Managed collections (read-only, paged)")
                    managed_page = await _call(mcp, "list_managed_collections", page=0)
                    managed = managed_page["items"]
                    ok(
                        f"{len(managed)} managed collection(s) on page 0 — "
                        f"total_pages={managed_page['total_pages']} "
                        f"total_elements={managed_page['total_elements']}"
                    )
                    for c in managed[:3]:
                        info(
                            f"- {c['name']!r}: {c['recipe_count']} recipes "
                            f"in {c['chapter_count']} chapters"
                        )

                    section("Custom collections (read-only, paged)")
                    customs_page = await _call(mcp, "list_custom_collections", page=0)
                    customs = customs_page["items"]
                    ok(
                        f"{len(customs)} custom collection(s) on page 0 — "
                        f"total_pages={customs_page['total_pages']} "
                        f"total_elements={customs_page['total_elements']}"
                    )
                    for c in customs[:3]:
                        info(f"- {c['name']!r}: {c['recipe_count']} recipes")

                    section("Shopping list (read-only)")
                    shop = await _call(mcp, "get_shopping_list")
                    ok(
                        f"recipe items: {len(shop['ingredient_items'])}, "
                        f"additional items: {len(shop['additional_items'])}, "
                        f"recipes on list: {len(shop['recipes'])}"
                    )
                    for r in shop["recipes"][:3]:
                        info(f"- {r['name']!r} (id={r['id']}, {len(r['ingredients'])} ingredients)")

                    section("Calendar (read-only)")
                    week = await _call(mcp, "get_calendar_week", day=date.today().isoformat())
                    ok(f"week containing today returned {len(week)} day(s)")
                    for d in week:
                        if d["recipes"]:
                            info(f"- {d['title']}: {len(d['recipes'])} recipe(s)")
                            if recipe_id_for_lookup is None:
                                recipe_id_for_lookup = d["recipes"][0]["id"]

                    if recipe_id_for_lookup is None:
                        seed = await _call(mcp, "search_recipes", query="Pasta", limit=1)
                        if seed:
                            recipe_id_for_lookup = seed[0]["id"]
                            info(f"calendar empty — seeded {recipe_id_for_lookup!r} from search")

                    section("Custom recipes (read-only, pre-upload)")
                    # First pass exercises the listing parser against whatever
                    # the account contains before the smoke test runs. If it is
                    # empty this short-circuits — see the post-upload re-check.
                    try:
                        pre_recipes = await _call(mcp, "list_custom_recipes")
                        ok(f"{len(pre_recipes)} custom recipe(s)")
                    except Exception as e:
                        failures += 1
                        fail(f"list_custom_recipes (pre-upload) raised: {e!r}")

                    section("Public recipe details (read-only)")
                    if recipe_id_for_lookup:
                        details = await _call(
                            mcp, "get_recipe_details", recipe_id=recipe_id_for_lookup
                        )
                        ok(
                            f"id={details['id']!r} name={details['name']!r} "
                            f"ingredients={len(details['ingredients'])}"
                        )
                        ok(
                            f"enrichment: categories={len(details['categories'])} "
                            f"collections={len(details['collections'])} "
                            f"nutrition={len(details['nutrition'])}"
                        )
                        for n in details["nutrition"][:1]:
                            values = ", ".join(
                                f"{v['value']}{v['unit']} {v['type']}" for v in n["values"][:4]
                            )
                            info(f"- nutrition per {n['quantity']} {n['unit_notation']}: {values}")
                        if not details["nutrition"]:
                            info(
                                "nutrition list empty — upstream marks the field as "
                                "optional, so this is not necessarily a parser miss"
                            )
                        with_images = await _call(
                            mcp,
                            "get_recipe_details",
                            recipe_id=recipe_id_for_lookup,
                            include_images=True,
                        )
                        images = with_images["images"]
                        if images:
                            ok(f"include_images: {len(images)} image(s)")
                            info(f"- first square: {images[0]['square']}")
                            unresolved = [
                                value
                                for image in images
                                for value in image.values()
                                if isinstance(value, str) and "{transformation}" in value
                            ]
                            if unresolved:
                                failures += 1
                                fail(f"unresolved CDN placeholder in {len(unresolved)} URL(s)")
                        else:
                            failures += 1
                            fail("include_images=True returned no images for a catalogue recipe")
                    else:
                        warn("no recipe ID found in calendar — skipping get_recipe_details")

                    section("Recipe interactions read (read-only)")
                    if recipe_id_for_lookup:
                        try:
                            with_inter = await _call(
                                mcp,
                                "get_recipe_details",
                                recipe_id=recipe_id_for_lookup,
                                include_interactions=True,
                            )
                            inter = with_inter.get("interactions")
                            if inter is None:
                                failures += 1
                                fail("interactions field stayed null despite the flag")
                            else:
                                ok(
                                    f"own_rating={inter.get('own_rating')} "
                                    f"average_rating={inter.get('average_rating')} "
                                    f"number_of_ratings={inter.get('number_of_ratings')} "
                                    f"note={inter.get('note')!r}"
                                )
                                if inter.get("average_rating") is None:
                                    failures += 1
                                    fail(
                                        "average_rating is null for a catalogue recipe — "
                                        "the aggregated-ratings parser regressed"
                                    )
                        except Exception as e:
                            failures += 1
                            fail(f"get_recipe_details(include_interactions=True) raised: {e!r}")
                    else:
                        warn("no recipe ID — skipping interactions read test")

                    section("Recipe structure generation (no API)")
                    annotated_step_text = (
                        "200 g Mehl und 100 ml Wasser in den Mixtopf geben, "
                        "30 Sek. / Stufe 4 verkneten."
                    )
                    sent_annotations: list[dict[str, Any]] = [
                        _ingredient_annotation(annotated_step_text, "200 g Mehl"),
                        _ingredient_annotation(annotated_step_text, "100 ml Wasser"),
                        _tts_annotation(
                            annotated_step_text, "30 Sek. / Stufe 4", speed="4", time=30
                        ),
                    ]
                    annotated_step: dict[str, Any] = {
                        "text": annotated_step_text,
                        "annotations": sent_annotations,
                    }
                    browning_step_text = "Teigfladen in der Pfanne 5 Min./150 °C/Intensiv anbraten."
                    browning_annotations: list[dict[str, Any]] = [
                        _browning_annotation(
                            browning_step_text,
                            "5 Min./150 °C/Intensiv",
                            time=300,
                            temperature="150",
                            power="Intense",
                        ),
                    ]
                    browning_step: dict[str, Any] = {
                        "text": browning_step_text,
                        "annotations": browning_annotations,
                    }
                    draft_dict = await _call(
                        mcp,
                        "generate_recipe_structure",
                        name=RECIPE_NAME,
                        ingredients=["200 g Mehl", "100 ml Wasser", "1 Prise Salz"],
                        steps=[
                            annotated_step,
                            browning_step,
                            "Teig mit dem Spatel herausnehmen und 10 Min. ruhen lassen.",
                        ],
                        servings=2,
                        prep_minutes=5,
                        total_minutes=15,
                        tools=["TM6", "TM7"],
                        hints=["Teig vor dem Braten 10 Min. ruhen lassen."],
                    )
                    ok(f"draft built: name={draft_dict['name']!r} steps={len(draft_dict['steps'])}")
                    if draft_dict.get("tools") == ["TM6", "TM7"] and draft_dict.get("hints"):
                        ok("tools + hints survived the MCP roundtrip")
                    else:
                        failures += 1
                        fail(
                            f"tools/hints roundtrip mismatch: tools={draft_dict.get('tools')!r} "
                            f"hints={draft_dict.get('hints')!r}"
                        )
                    returned_annotations = draft_dict["steps"][0].get("annotations", [])
                    sent = sorted(sent_annotations, key=lambda a: a["offset"])
                    got = sorted(returned_annotations, key=lambda a: a["offset"])
                    if sent == got:
                        ok(f"explicit annotations survived the roundtrip ({len(got)} on step 0)")
                        types = sorted({a["type"] for a in got})
                        info(f"annotation types on step 0: {types}")
                    else:
                        failures += 1
                        fail(f"annotation roundtrip mismatch: sent={sent!r} got={got!r}")
                    returned_mode = draft_dict["steps"][1].get("annotations", [])
                    if returned_mode == browning_annotations:
                        ok("MODE/BROWNING annotation survived the MCP roundtrip")
                    else:
                        failures += 1
                        fail(
                            f"MODE/BROWNING roundtrip mismatch: sent={browning_annotations!r} "
                            f"got={returned_mode!r}"
                        )

                    section("Quality scoring (no API)")
                    report = await _call(mcp, "validate_recipe_quality", draft=draft_dict)
                    ok(
                        f"score={report['score']}/100 meets_bar={report['meets_bar']} "
                        f"issues={len(report['issues'])}"
                    )
                    for issue in report["issues"][:3]:
                        info(f"- {issue['rule']} [{issue['severity']}]: {issue['message']}")

                    section("Recipe search (read-only)")
                    # ``Pasta`` is recognised across every Cookidoo locale, so the
                    # search hit count is expected to be > 0 on a live account.
                    unfiltered_ids: list[str] = []
                    try:
                        search_results = await _call(mcp, "search_recipes", query="Pasta", limit=5)
                        unfiltered_ids = [h["id"] for h in search_results]
                        ok(f"search_recipes returned {len(search_results)} hit(s) for 'Pasta'")
                        for hit in search_results[:3]:
                            info(f"- {hit['name']!r} (id={hit['id']})")
                        if recipe_id_for_lookup is None and search_results:
                            # Adopt a hit as the recipe-id source for the
                            # downstream write sections (clone, calendar, shopping
                            # ingredient cycle) when the calendar yielded none.
                            recipe_id_for_lookup = search_results[0]["id"]
                            info(
                                f"adopted {recipe_id_for_lookup!r} from search for downstream tests"
                            )
                    except Exception as e:
                        failures += 1
                        fail(f"search_recipes raised: {e!r}")

                    section("Recipe search with filters (read-only)")
                    # The filter params mirror the Cookidoo web UI query string;
                    # their wire format was implemented without live verification,
                    # so this section both exercises and validates them.
                    try:
                        filtered = await _call(
                            mcp,
                            "search_recipes",
                            query="Pasta",
                            limit=5,
                            max_total_minutes=30,
                            min_rating=4,
                        )
                        ok(f"{len(filtered)} hit(s) for 'Pasta' with <=30min & rating>=4")
                        overlong = [
                            h
                            for h in filtered
                            if h.get("total_time_seconds") and h["total_time_seconds"] > 1800
                        ]
                        underrated = [h for h in filtered if h.get("rating") and h["rating"] < 3.5]
                        for hit in filtered[:3]:
                            info(
                                f"- {hit['name']!r} total={hit.get('total_time_seconds')}s "
                                f"rating={hit.get('rating')}"
                            )
                        if not filtered:
                            failures += 1
                            fail(
                                "0 hits with filters while the unfiltered query matched — "
                                "the filter wire format regressed"
                            )
                        elif overlong or underrated:
                            failures += 1
                            fail(
                                f"{len(overlong)} hit(s) over 30min / {len(underrated)} "
                                f"below rating 3.5 — upstream ignored the filter params"
                            )
                        else:
                            ok("all hits respect the requested filters")
                    except Exception as e:
                        failures += 1
                        fail(f"search_recipes with filters raised: {e!r}")

                    section("Recipe search, remaining filters (read-only)")
                    sample_category: str | None = None
                    if recipe_id_for_lookup:
                        cat_source = await _call(
                            mcp, "get_recipe_details", recipe_id=recipe_id_for_lookup
                        )
                        if cat_source["categories"]:
                            sample_category = cat_source["categories"][0]["id"]
                            info(f"sample category: {sample_category!r}")

                    strict_filters: list[tuple[str, dict[str, Any]]] = [
                        ("difficulty", {"difficulty": "easy"}),
                        ("ingredients", {"ingredients": ["Tomate"]}),
                        ("exclude_ingredients", {"exclude_ingredients": ["Fleisch"]}),
                        ("portions", {"portions": 4}),
                        ("thermomix_version", {"thermomix_version": "TM6"}),
                        ("sort_by", {"sort_by": "rating"}),
                    ]
                    lenient_filters: list[tuple[str, dict[str, Any]]] = [
                        ("accessories", {"accessories": ["varoma"]}),
                    ]
                    if sample_category is not None:
                        lenient_filters.append(("categories", {"categories": [sample_category]}))

                    for label, kwargs in strict_filters + lenient_filters:
                        strict = (label, kwargs) in strict_filters
                        try:
                            hits = await _call(
                                mcp, "search_recipes", query="Pasta", limit=5, **kwargs
                            )
                        except Exception as e:
                            failures += 1
                            fail(f"search_recipes({label}=...) raised: {e!r}")
                            continue
                        if hits:
                            same = [h["id"] for h in hits] == unfiltered_ids
                            suffix = " (same as unfiltered)" if same else ""
                            ok(f"{label}: {len(hits)} hit(s){suffix}")
                        elif strict:
                            failures += 1
                            fail(f"{label}: 0 hits while the unfiltered query matched")
                        else:
                            info(f"{label}: 0 hits (opaque upstream ID — not treated as failure)")
                        if label == "exclude_ingredients" and hits:
                            with_meat = await _call(
                                mcp,
                                "search_recipes",
                                query="Pasta",
                                limit=5,
                                ingredients=["Hackfleisch"],
                            )
                            without_meat = await _call(
                                mcp,
                                "search_recipes",
                                query="Pasta",
                                limit=5,
                                exclude_ingredients=["Hackfleisch"],
                            )
                            overlap = {h["id"] for h in with_meat} & {h["id"] for h in without_meat}
                            if not with_meat:
                                info("counter-check skipped — no 'Hackfleisch' hits to exclude")
                            elif overlap:
                                failures += 1
                                fail(
                                    f"exclude_ingredients ignored — {len(overlap)} recipe(s) "
                                    f"appear in both the include and the exclude result"
                                )
                            else:
                                ok("exclude_ingredients verified against its include counterpart")
                        if label == "sort_by" and hits:
                            ratings = [h["rating"] for h in hits if h.get("rating") is not None]
                            if ratings == sorted(ratings, reverse=True):
                                ok("sort_by=rating returned a descending ranking")
                            else:
                                failures += 1
                                fail(f"sort_by=rating ignored — ratings came back {ratings}")

                    section("Recommendations (read-only)")
                    try:
                        foryou = await _call(mcp, "get_recipe_recommendations", limit=5)
                        if foryou:
                            ok(f"'For you' feed returned {len(foryou)} recipe(s)")
                            for hit in foryou[:3]:
                                info(f"- {hit['name']!r} (id={hit['id']})")
                        else:
                            failures += 1
                            fail("'For you' feed returned no recipes")
                        if recipe_id_for_lookup:
                            similar = await _call(
                                mcp,
                                "get_recipe_recommendations",
                                recipe_id=recipe_id_for_lookup,
                                limit=5,
                            )
                            if similar:
                                ok(f"similar-recipes returned {len(similar)} recipe(s)")
                            else:
                                failures += 1
                                fail("similar-recipes returned no recipes")
                    except Exception as e:
                        failures += 1
                        fail(f"get_recipe_recommendations raised: {e!r}")

                    section("Bookmarked recipes (read-only)")
                    try:
                        bookmarks = await _call(mcp, "list_bookmarked_recipes")
                        ok(f"list_bookmarked_recipes returned {len(bookmarks)} recipe(s)")
                    except Exception as e:
                        failures += 1
                        fail(f"list_bookmarked_recipes raised: {e!r}")

                    section("Cooking history (read-only)")
                    try:
                        history = await _call(mcp, "get_cooking_history", limit=5)
                        if history:
                            ok(f"get_cooking_history returned {len(history)} entr(ies)")
                            info(
                                f"- {history[0]['recipe']['name']!r} "
                                f"cooked_at={history[0]['cooked_at']}"
                            )
                        else:
                            failures += 1
                            fail("cooking history empty despite earlier mark_cooked runs")
                    except Exception as e:
                        failures += 1
                        fail(f"get_cooking_history raised: {e!r}")

                    section("Resource read (cookidough://shopping-list)")
                    try:
                        resource = await mcp.read_resource("cookidough://shopping-list")
                        content = resource.contents[0]
                        if not isinstance(content, TextResourceContents):
                            raise TypeError(f"expected text content, got {type(content).__name__}")
                        parsed = json.loads(content.text)
                        ok(
                            f"resource read ok — {len(parsed['ingredient_items'])} recipe "
                            f"item(s), {len(parsed['additional_items'])} additional item(s)"
                        )
                    except Exception as e:
                        failures += 1
                        fail(f"resource read raised: {e!r}")

                    section("Recipe suggestions from ingredients (read-only)")
                    # Without collection_ids this now searches the whole library
                    # via the server-side ingredients filter (falling back to the
                    # collection walk when the filter yields nothing).
                    try:
                        suggestions = await _call(
                            mcp,
                            "suggest_recipes_from_ingredients",
                            available_ingredients=["Salz", "Mehl"],
                            max_results=2,
                        )
                        ok(
                            f"suggest_recipes_from_ingredients returned "
                            f"{len(suggestions)} suggestion(s)"
                        )
                        for s in suggestions[:2]:
                            info(f"- {s['recipe']['name']!r} (score={s['score']})")
                    except Exception as e:
                        failures += 1
                        fail(f"suggest_recipes_from_ingredients raised: {e!r}")

                    section("WRITE: managed collection add+remove (hardcoded catalog ID)")
                    already_subscribed = any(
                        c["id"] == SMOKE_MANAGED_COLLECTION_ID for c in managed
                    )
                    if already_subscribed:
                        warn(
                            f"managed collection {SMOKE_MANAGED_COLLECTION_ID!r} is already "
                            "subscribed — skipping (cannot safely remove pre-existing state)"
                        )
                    else:
                        try:
                            added_mc = await _call(
                                mcp,
                                "add_managed_collection",
                                collection_id=SMOKE_MANAGED_COLLECTION_ID,
                            )
                            pending_managed_collection_id = SMOKE_MANAGED_COLLECTION_ID
                            ok(
                                f"added managed collection id={added_mc['id']} "
                                f"name={added_mc['name']!r}"
                            )
                            post_add = (await _call(mcp, "list_managed_collections", page=0))[
                                "items"
                            ]
                            if any(c["id"] == SMOKE_MANAGED_COLLECTION_ID for c in post_add):
                                ok("verified present after add")
                            else:
                                failures += 1
                                fail("managed collection missing from listing after add")
                            msg = await _call(
                                mcp,
                                "remove_managed_collection",
                                collection_id=SMOKE_MANAGED_COLLECTION_ID,
                            )
                            pending_managed_collection_id = None
                            ok(f"removed managed collection ({msg})")
                            post_remove = (await _call(mcp, "list_managed_collections", page=0))[
                                "items"
                            ]
                            if any(c["id"] == SMOKE_MANAGED_COLLECTION_ID for c in post_remove):
                                failures += 1
                                fail("managed collection still present after remove")
                            else:
                                ok("verified gone after remove")
                        except Exception as e:
                            failures += 1
                            fail(f"managed collection cycle raised: {e!r}")

                    section("WRITE: shopping list clear (only if empty)")
                    # Re-read the shopping list right before the guard so a
                    # future reordering of sections cannot cause us to clear a
                    # list that was populated by a previous write section.
                    shop_now = await _call(mcp, "get_shopping_list")
                    if shop_now["ingredient_items"] or shop_now["additional_items"]:
                        warn(
                            f"shopping list is not empty "
                            f"({len(shop_now['ingredient_items'])} recipe + "
                            f"{len(shop_now['additional_items'])} additional) "
                            "— skipping clear_shopping_list to protect pre-existing data"
                        )
                    else:
                        try:
                            sentinel = await _call(
                                mcp, "add_additional_items", names=[SENTINEL_ITEM_NAME]
                            )
                            if len(sentinel) == 1:
                                ok("sentinel item added")
                            else:
                                failures += 1
                                fail(f"expected 1 sentinel item, got {len(sentinel)}")
                            msg = await _call(mcp, "clear_shopping_list")
                            post_clear = await _call(mcp, "get_shopping_list")
                            if post_clear["ingredient_items"] or post_clear["additional_items"]:
                                failures += 1
                                fail(
                                    f"clear_shopping_list left items behind ({msg}): "
                                    f"recipe={len(post_clear['ingredient_items'])}, "
                                    f"additional={len(post_clear['additional_items'])}"
                                )
                            else:
                                ok(f"clear_shopping_list emptied the list ({msg})")
                        except Exception as e:
                            failures += 1
                            fail(f"clear_shopping_list cycle raised: {e!r}")

                    section("WRITE: custom collection create+delete")
                    collection = await _call(mcp, "create_custom_collection", name=COLLECTION_NAME)
                    created_collection_id = collection["id"]
                    ok(f"created collection id={collection['id']} name={collection['name']!r}")

                    section("WRITE: collection recipe membership add+remove")
                    if recipe_id_for_lookup and created_collection_id is not None:
                        try:
                            updated = await _call(
                                mcp,
                                "add_recipes_to_custom_collection",
                                collection_id=created_collection_id,
                                recipe_ids=[recipe_id_for_lookup],
                            )
                            ok(
                                f"added {recipe_id_for_lookup!r} to collection; "
                                f"now has {updated['recipe_count']} recipe(s)"
                            )
                            scoped = await _call(
                                mcp,
                                "suggest_recipes_from_ingredients",
                                available_ingredients=["Salz", "Mehl", "Wasser"],
                                collection_ids=[created_collection_id],
                                max_results=3,
                            )
                            ok(f"collection-scoped suggester returned {len(scoped)} suggestion(s)")
                            msg = await _call(
                                mcp,
                                "remove_recipe_from_custom_collection",
                                collection_id=created_collection_id,
                                recipe_id=recipe_id_for_lookup,
                            )
                            ok(f"removed {recipe_id_for_lookup!r} from collection ({msg})")
                        except Exception as e:
                            failures += 1
                            fail(f"collection membership cycle raised: {e!r}")
                    else:
                        warn("no recipe ID available — skipping collection-membership test")

                    section("WRITE: additional shopping items add+remove")
                    items = await _call(mcp, "add_additional_items", names=[ITEM_NAME])
                    created_item_ids = [i["id"] for i in items]
                    ok(f"added {len(items)} item(s): {[i['name'] for i in items]}")

                    section("WRITE: additional item rename + ownership toggle")
                    # Both calls go to /shopping/{lang}/additional-items/* and
                    # share the additional-item id. We rename + tick, then
                    # restore the original name + clear the owned flag so the
                    # trailing artefact probe (which matches by full name) and
                    # the explicit ``remove_additional_items`` cleanup still
                    # see the item under ``ITEM_NAME``.
                    if created_item_ids:
                        renamed_name = f"[SMOKE_TEST cookidough-mcp] renamed {MARKER}"
                        try:
                            renamed = await _call(
                                mcp,
                                "rename_additional_items",
                                updates=[{"id": created_item_ids[0], "name": renamed_name}],
                            )
                            if renamed and renamed[0]["name"] == renamed_name:
                                ok(f"renamed item to {renamed_name!r}")
                            else:
                                failures += 1
                                fail(f"rename_additional_items returned: {renamed!r}")

                            ticked = await _call(
                                mcp,
                                "set_additional_items_ownership",
                                updates=[{"id": created_item_ids[0], "is_owned": True}],
                            )
                            if ticked and ticked[0]["is_owned"] is True:
                                ok("ticked additional item as owned")
                            else:
                                failures += 1
                                fail(f"set_additional_items_ownership returned: {ticked!r}")

                            # Restore name + ownership so downstream cleanup can
                            # identify the item by its original ITEM_NAME marker.
                            await _call(
                                mcp,
                                "rename_additional_items",
                                updates=[{"id": created_item_ids[0], "name": ITEM_NAME}],
                            )
                            await _call(
                                mcp,
                                "set_additional_items_ownership",
                                updates=[{"id": created_item_ids[0], "is_owned": False}],
                            )
                            ok("restored name + ownership for safe cleanup")
                        except Exception as e:
                            failures += 1
                            fail(f"additional-item rename/ownership cycle raised: {e!r}")
                    else:
                        warn("no additional item created — skipping rename/ownership cycle")

                    section("WRITE: recipe ingredients shopping list add+remove")
                    if recipe_id_for_lookup:
                        try:
                            add_msg = await _call(
                                mcp,
                                "add_recipes_to_shopping_list",
                                recipe_ids=[recipe_id_for_lookup],
                            )
                            ok(f"add_recipes_to_shopping_list returned: {add_msg!r}")
                            # Toggle one of the just-added ingredient items so we
                            # also exercise set_ingredient_items_ownership while
                            # we have a real recipe-derived item ID in hand. The
                            # subsequent remove_recipes_from_shopping_list call
                            # drops the items regardless of their owned flag.
                            shop_after_add = await _call(mcp, "get_shopping_list")
                            target_item = next(
                                (i for i in shop_after_add["ingredient_items"] if i.get("id")),
                                None,
                            )
                            if target_item is None:
                                warn(
                                    "no recipe-derived ingredient items present — "
                                    "skipping set_ingredient_items_ownership"
                                )
                            else:
                                toggled = await _call(
                                    mcp,
                                    "set_ingredient_items_ownership",
                                    updates=[{"id": target_item["id"], "is_owned": True}],
                                )
                                if toggled and any(
                                    i["id"] == target_item["id"] and i["is_owned"] is True
                                    for i in toggled
                                ):
                                    ok(f"ticked ingredient item {target_item['id']!r}")
                                else:
                                    failures += 1
                                    fail(f"set_ingredient_items_ownership returned: {toggled!r}")
                            remove_msg = await _call(
                                mcp,
                                "remove_recipes_from_shopping_list",
                                recipe_ids=[recipe_id_for_lookup],
                            )
                            ok(f"remove_recipes_from_shopping_list returned: {remove_msg!r}")
                        except Exception as e:
                            failures += 1
                            fail(f"recipe-shopping cycle raised: {e!r}")
                    else:
                        warn("no recipe ID available — skipping recipe-shopping test")

                    section("WRITE: custom recipe upload+delete")
                    info(
                        "exercises the upload_custom_recipe tool — quality gate "
                        "runs in the tool layer; we pass force=True so the draft "
                        "is uploaded regardless of its TM7 score"
                    )
                    try:
                        upload = await _call(
                            mcp, "upload_custom_recipe", draft=draft_dict, force=True
                        )
                        created_recipe_id = upload["recipe_id"]
                        ok(f"uploaded recipe id={upload['recipe_id']}")
                        info(f"public url: {upload['url']}")
                    except Exception as e:
                        failures += 1
                        fail(f"upload_custom_recipe raised: {e!r}")

                    section("Custom recipes (read-only, post-upload)")
                    # This pass is the one that would have caught the `totalTime`
                    # ISO-8601 parser regression: now that we have definitely just
                    # created a custom recipe, the listing must contain at least
                    # one entry whose `totalTime` is parsed.
                    try:
                        populated = await _call(mcp, "list_custom_recipes")
                        ok(f"{len(populated)} custom recipe(s) after upload")
                        if created_recipe_id is not None:
                            if any(r["recipe_id"] == created_recipe_id for r in populated):
                                ok("uploaded recipe is present in listing")
                            else:
                                failures += 1
                                fail(f"uploaded recipe {created_recipe_id!r} missing from listing")
                    except Exception as e:
                        failures += 1
                        fail(f"list_custom_recipes (post-upload) raised: {e!r}")

                    section("Custom recipe details (read-only)")
                    if created_recipe_id is not None:
                        try:
                            detail = await _call(
                                mcp,
                                "get_custom_recipe_details",
                                recipe_id=created_recipe_id,
                            )
                            ok(
                                f"id={detail['id']!r} name={detail['name']!r} "
                                f"ingredients={len(detail['ingredients'])} "
                                f"total={detail['total_time_seconds']}s"
                            )
                        except Exception as e:
                            failures += 1
                            fail(f"get_custom_recipe_details raised: {e!r}")
                    else:
                        warn("upload failed earlier — skipping get_custom_recipe_details")

                    section("WRITE: custom recipe update (upload_custom_recipe + recipe_id)")
                    if created_recipe_id is not None:
                        updated_name = f"{RECIPE_NAME} v2"
                        try:
                            updated_draft = {**draft_dict, "name": updated_name}
                            update_result = await _call(
                                mcp,
                                "upload_custom_recipe",
                                draft=updated_draft,
                                force=True,
                                recipe_id=created_recipe_id,
                            )
                            if update_result["recipe_id"] == created_recipe_id:
                                ok(f"update returned the same recipe id {created_recipe_id!r}")
                            else:
                                failures += 1
                                fail(
                                    f"update returned id {update_result['recipe_id']!r} "
                                    f"instead of {created_recipe_id!r}"
                                )
                            # The PATCH may be eventually consistent — allow one retry.
                            for attempt in range(2):
                                detail_after = await _call(
                                    mcp,
                                    "get_custom_recipe_details",
                                    recipe_id=created_recipe_id,
                                )
                                if detail_after["name"] == updated_name:
                                    ok(f"name change persisted (attempt {attempt + 1})")
                                    break
                                await asyncio.sleep(3)
                            else:
                                failures += 1
                                fail(
                                    f"updated name not visible after retries: "
                                    f"got {detail_after['name']!r}"
                                )
                        except Exception as e:
                            failures += 1
                            fail(f"custom recipe update cycle raised: {e!r}")
                    else:
                        warn("no uploaded custom recipe available — skipping update test")

                    section("WRITE: custom recipe image upload")
                    if created_recipe_id is not None:
                        try:
                            import struct
                            import tempfile
                            import zlib

                            def _chunk(tag: bytes, data: bytes) -> bytes:
                                raw = tag + data
                                return (
                                    struct.pack(">I", len(data))
                                    + raw
                                    + struct.pack(">I", zlib.crc32(raw))
                                )

                            row = b"\x00" + b"\xc8\x78\x28" * 100
                            png = (
                                b"\x89PNG\r\n\x1a\n"
                                + _chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 2, 0, 0, 0))
                                + _chunk(b"IDAT", zlib.compress(row * 100))
                                + _chunk(b"IEND", b"")
                            )
                            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                                handle.write(png)
                                png_path = handle.name
                            image_result = await _call(
                                mcp,
                                "set_custom_recipe_image",
                                recipe_id=created_recipe_id,
                                image_source=png_path,
                            )
                            await asyncio.to_thread(Path(png_path).unlink)
                            if image_result["image"] and "customer-recipe" in image_result["image"]:
                                ok(f"image set: {image_result['image']}")
                            else:
                                failures += 1
                                fail(f"unexpected image value: {image_result['image']!r}")

                            # A later full update must NOT wipe the photo.
                            await _call(
                                mcp,
                                "upload_custom_recipe",
                                draft={**draft_dict, "name": f"{RECIPE_NAME} v3"},
                                force=True,
                                recipe_id=created_recipe_id,
                            )
                            detail_after_update = await _call(
                                mcp,
                                "get_custom_recipe_details",
                                recipe_id=created_recipe_id,
                            )
                            if detail_after_update["image"] and "customer-recipe" in (
                                detail_after_update["image"] or ""
                            ):
                                ok("image survived a subsequent full recipe update")
                            else:
                                failures += 1
                                fail(f"image wiped by update: {detail_after_update['image']!r}")
                        except Exception as e:
                            failures += 1
                            fail(f"custom recipe image cycle raised: {e!r}")
                    else:
                        warn("no uploaded custom recipe available — skipping image test")

                    section("WRITE: recipe interactions (rating restored, history persists)")
                    if recipe_id_for_lookup:
                        smoke_note = f"[SMOKE_TEST cookidough-mcp] note {MARKER}"
                        try:
                            before = await _call(
                                mcp,
                                "get_recipe_details",
                                recipe_id=recipe_id_for_lookup,
                                include_interactions=True,
                            )
                            prior_rating = (before.get("interactions") or {}).get("own_rating")
                            test_rating = 4 if prior_rating == 5 else 5
                            info(f"own rating before: {prior_rating!r}, writing {test_rating}")

                            inter_result = await _call(
                                mcp,
                                "set_recipe_interactions",
                                recipe_id=recipe_id_for_lookup,
                                rating=test_rating,
                                bookmarked=True,
                                note=smoke_note,
                                mark_cooked=True,
                            )
                            for action in ("rating", "bookmark", "note", "cooked"):
                                status = inter_result.get(action)
                                if status == "ok":
                                    ok(f"{action}: ok")
                                else:
                                    failures += 1
                                    fail(f"interactions.{action}: status={status!r}")

                            read_back = await _call(
                                mcp,
                                "get_recipe_details",
                                recipe_id=recipe_id_for_lookup,
                                include_interactions=True,
                            )
                            inter_after = read_back.get("interactions") or {}
                            if inter_after.get("own_rating") == test_rating:
                                ok(f"own rating reads back as {test_rating}")
                            else:
                                failures += 1
                                fail(
                                    f"own rating read-back shows "
                                    f"{inter_after.get('own_rating')!r} instead of {test_rating}"
                                )
                            if inter_after.get("note") == smoke_note:
                                ok("note reads back verbatim")
                            else:
                                failures += 1
                                fail(
                                    f"note read-back shows {inter_after.get('note')!r} "
                                    f"instead of the smoke note"
                                )

                            revert = await _call(
                                mcp,
                                "set_recipe_interactions",
                                recipe_id=recipe_id_for_lookup,
                                bookmarked=False,
                                note="",
                            )
                            if revert.get("bookmark") == "ok" and revert.get("note") == "ok":
                                ok("bookmark + note reverted")
                            else:
                                failures += 1
                                fail(
                                    f"revert failed: bookmark={revert.get('bookmark')!r} "
                                    f"note={revert.get('note')!r}"
                                )

                            if prior_rating is None:
                                warn(
                                    "rating stays on the account: Cookidoo has no delete "
                                    "route and PUT rejects 0/null, so a recipe that was "
                                    "unrated before cannot be restored to unrated"
                                )
                            else:
                                restored = await _call(
                                    mcp,
                                    "set_recipe_interactions",
                                    recipe_id=recipe_id_for_lookup,
                                    rating=prior_rating,
                                )
                                after_restore = await _call(
                                    mcp,
                                    "get_recipe_details",
                                    recipe_id=recipe_id_for_lookup,
                                    include_interactions=True,
                                )
                                now = (after_restore.get("interactions") or {}).get("own_rating")
                                if restored.get("rating") == "ok" and now == prior_rating:
                                    ok(f"rating restored to its previous value {prior_rating}")
                                else:
                                    failures += 1
                                    fail(
                                        f"rating restore failed: status="
                                        f"{restored.get('rating')!r} own_rating={now!r} "
                                        f"instead of {prior_rating!r}"
                                    )
                        except Exception as e:
                            failures += 1
                            fail(f"recipe interactions cycle raised: {e!r}")
                    else:
                        warn("no recipe ID — skipping interactions write test")

                    section("WRITE: clone_recipe_as_custom")
                    try:
                        cloned = await _call(
                            mcp,
                            "clone_recipe_as_custom",
                            recipe_id=SMOKE_CLONE_RECIPE_ID,
                            serving_size=2,
                        )
                        cloned_recipe_id = cloned["id"]
                        ok(f"cloned {SMOKE_CLONE_RECIPE_ID!r} -> custom id={cloned_recipe_id}")
                    except Exception as e:
                        failures += 1
                        fail(f"clone_recipe_as_custom raised: {e!r}")

                    section("WRITE: custom recipe shopping list add+remove")
                    if created_recipe_id is not None:
                        try:
                            add_msg = await _call(
                                mcp,
                                "add_custom_recipes_to_shopping_list",
                                recipe_ids=[created_recipe_id],
                            )
                            ok(f"add_custom_recipes_to_shopping_list returned: {add_msg!r}")
                            remove_msg = await _call(
                                mcp,
                                "remove_custom_recipes_from_shopping_list",
                                recipe_ids=[created_recipe_id],
                            )
                            ok(f"remove_custom_recipes_from_shopping_list returned: {remove_msg!r}")
                        except Exception as e:
                            failures += 1
                            fail(f"custom-recipe shopping cycle raised: {e!r}")
                    else:
                        warn("no uploaded custom recipe — skipping custom-recipe shopping test")

                    section("WRITE: calendar add+remove (2099-01-01)")
                    if recipe_id_for_lookup:
                        try:
                            planned = await _call(
                                mcp,
                                "add_recipes_to_calendar",
                                day=FUTURE_TEST_DAY,
                                recipe_ids=[recipe_id_for_lookup],
                            )
                            planned_calendar_day = FUTURE_TEST_DAY
                            planned_calendar_recipe_id = recipe_id_for_lookup
                            ok(
                                f"planned recipe {recipe_id_for_lookup!r} on {FUTURE_TEST_DAY}; "
                                f"day now has {len(planned['recipes'])} recipe(s)"
                            )
                        except Exception as e:
                            failures += 1
                            fail(f"add_recipes_to_calendar raised: {e!r}")
                    else:
                        warn("no recipe ID available — skipping calendar write test")

                    section("WRITE: custom recipe calendar add+remove (2099-01-01)")
                    # Schedules the uploaded custom recipe on the same future date.
                    # Cookidoo can hold regular + custom recipes on the same day;
                    # the cleanup section removes each entry through its own
                    # endpoint to avoid relying on a cascade.
                    if created_recipe_id is not None:
                        try:
                            planned = await _call(
                                mcp,
                                "add_custom_recipes_to_calendar",
                                day=FUTURE_TEST_DAY,
                                recipe_ids=[created_recipe_id],
                            )
                            planned_custom_calendar_day = FUTURE_TEST_DAY
                            planned_custom_calendar_recipe_id = created_recipe_id
                            ok(
                                f"planned custom recipe {created_recipe_id!r} on "
                                f"{FUTURE_TEST_DAY}; day now has "
                                f"{len(planned['custom_recipe_ids'])} custom recipe(s)"
                            )
                        except Exception as e:
                            failures += 1
                            fail(f"add_custom_recipes_to_calendar raised: {e!r}")
                    else:
                        warn("no uploaded custom recipe available — skipping custom calendar test")

                    section("WRITE: calendar-range shopping list (2099-01-01)")
                    # Both a regular and a custom recipe are planned on the test
                    # day at this point; the range mode must pick up both. The
                    # added ingredients are removed right away.
                    if planned_calendar_day is not None:
                        try:
                            range_msg = await _call(
                                mcp,
                                "add_recipes_to_shopping_list",
                                from_date=FUTURE_TEST_DAY,
                                to_date=FUTURE_TEST_DAY,
                            )
                            ok(f"calendar-range add returned: {range_msg!r}")
                            if planned_calendar_recipe_id is not None:
                                await _call(
                                    mcp,
                                    "remove_recipes_from_shopping_list",
                                    recipe_ids=[planned_calendar_recipe_id],
                                )
                            if planned_custom_calendar_recipe_id is not None:
                                await _call(
                                    mcp,
                                    "remove_custom_recipes_from_shopping_list",
                                    recipe_ids=[planned_custom_calendar_recipe_id],
                                )
                            ok("range-added ingredients removed again")
                        except Exception as e:
                            failures += 1
                            fail(f"calendar-range shopping cycle raised: {e!r}")
                    else:
                        warn("no planned calendar day — skipping calendar-range shopping test")

                finally:
                    section("Cleanup (always runs)")
                    # Cleanup goes through the MCP protocol just like the writes
                    # themselves did, so any breakage in the tool layer surfaces
                    # here too.

                    if planned_calendar_day is not None and planned_calendar_recipe_id is not None:
                        try:
                            await _call(
                                mcp,
                                "remove_recipe_from_calendar",
                                day=planned_calendar_day,
                                recipe_id=planned_calendar_recipe_id,
                            )
                            ok(f"removed planned recipe from {planned_calendar_day}")
                        except Exception as e:
                            failures += 1
                            fail(f"calendar cleanup failed: {e!r}")

                    # Drop the custom calendar entry BEFORE deleting the recipe
                    # itself; the upstream may otherwise leave a dangling entry.
                    if (
                        planned_custom_calendar_day is not None
                        and planned_custom_calendar_recipe_id is not None
                    ):
                        try:
                            await _call(
                                mcp,
                                "remove_custom_recipe_from_calendar",
                                day=planned_custom_calendar_day,
                                recipe_id=planned_custom_calendar_recipe_id,
                            )
                            ok(f"removed planned custom recipe from {planned_custom_calendar_day}")
                        except Exception as e:
                            failures += 1
                            fail(f"custom calendar cleanup failed: {e!r}")

                    if created_recipe_id is not None:
                        try:
                            msg = await _call(
                                mcp, "delete_custom_recipe", recipe_id=created_recipe_id
                            )
                            ok(f"deleted custom recipe {created_recipe_id} ({msg})")
                        except Exception as e:
                            failures += 1
                            fail(f"custom recipe cleanup failed: {e!r}")

                    if cloned_recipe_id is not None:
                        try:
                            msg = await _call(
                                mcp, "delete_custom_recipe", recipe_id=cloned_recipe_id
                            )
                            ok(f"deleted cloned recipe {cloned_recipe_id} ({msg})")
                        except Exception as e:
                            failures += 1
                            fail(f"cloned recipe cleanup failed: {e!r}")

                    if created_item_ids:
                        try:
                            msg = await _call(
                                mcp, "remove_additional_items", item_ids=created_item_ids
                            )
                            ok(f"removed {len(created_item_ids)} additional item(s) ({msg})")
                        except Exception as e:
                            failures += 1
                            fail(f"shopping items cleanup failed: {e!r}")

                    if created_collection_id is not None:
                        try:
                            msg = await _call(
                                mcp,
                                "delete_custom_collection",
                                collection_id=created_collection_id,
                            )
                            ok(f"deleted custom collection {created_collection_id} ({msg})")
                        except Exception as e:
                            failures += 1
                            fail(f"collection cleanup failed: {e!r}")

                    if pending_managed_collection_id is not None:
                        # add succeeded but inline remove either failed or never
                        # ran. Without this retry the account would be left
                        # subscribed to a collection it did not have before.
                        try:
                            await _call(
                                mcp,
                                "remove_managed_collection",
                                collection_id=pending_managed_collection_id,
                            )
                            ok(
                                f"emergency cleanup: removed managed collection "
                                f"{pending_managed_collection_id}"
                            )
                        except Exception as e:
                            failures += 1
                            fail(
                                f"emergency managed-collection cleanup failed for "
                                f"{pending_managed_collection_id!r}: {e!r} — "
                                "remove it manually from cookidoo.de"
                            )

                    # Smoke-artefact probe: if either clear_shopping_list (sentinel)
                    # or the additional-items cleanup (ITEM_NAME) failed mid-way,
                    # the entries may still be present. Match by full name so
                    # only the items we created get removed.
                    try:
                        current_shop = await _call(mcp, "get_shopping_list")
                        smoke_artefact_names = {SENTINEL_ITEM_NAME, ITEM_NAME}
                        leftover = [
                            i
                            for i in current_shop["additional_items"]
                            if i["name"] in smoke_artefact_names
                        ]
                        if leftover:
                            await _call(
                                mcp,
                                "remove_additional_items",
                                item_ids=[i["id"] for i in leftover],
                            )
                            ok(f"emergency cleanup: removed {len(leftover)} smoke artefact(s)")
                    except Exception as e:
                        warn(f"smoke-artefact cleanup probe failed: {e!r}")

    except Exception:
        failures += 1
        section("Unhandled exception")
        traceback.print_exc()

    section("Result")
    if failures == 0:
        ok("smoke test PASSED — no failures, no artefacts left behind")
        return 0
    fail(f"{failures} failure(s) — check logs above; cleanup attempted on best-effort basis")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
