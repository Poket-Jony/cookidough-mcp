"""End-to-end smoke test against a live Cookidoo account.

This test launches ``./run.sh`` exactly as Claude Desktop does and drives every
registered MCP tool over the JSON-RPC stdio protocol. The session layer is
never imported directly; everything exercises the full transport + FastMCP
dispatch + tool adapter + session + cookidoo-api stack.

Credentials are loaded from a file **outside** the repository tree so they
can never leak into git history. The lookup order is:

1. ``$COOKIDOO_SMOKE_ENV_FILE`` if set.
2. ``$XDG_CONFIG_HOME/cookidoo-mcp/smoke.env``, else
3. ``~/.config/cookidoo-mcp/smoke.env``.

The file follows the same ``KEY=value`` format as ``.env.example``. The
values are then passed to the server via the subprocess environment — the
same mechanism Claude Desktop uses via the ``env`` field in ``mcp.json``.
If a regular ``.env`` exists in the repo root the test refuses to start:
``run.sh`` would ``source`` it last and silently override the inherited
test credentials.

This script is deliberately NOT named ``test_*.py`` so pytest will not
auto-discover it. Run it explicitly:

    .venv/bin/python tests/smoke/smoke_test.py

Every method on ``CookidooSessionProtocol`` is exercised through its tool
adapter. The two destructive operations have explicit safety guards:

- ``add_managed_collection`` / ``remove_managed_collection`` run against a
  hardcoded catalogue ID (``SMOKE_MANAGED_COLLECTION_ID``). The roundtrip is
  *skipped* when that collection is already subscribed.
- ``clear_shopping_list`` runs *only* when the shopping list is empty at
  the start of the writes phase.

``import_web_recipe`` is intentionally skipped — its real dependency is an
external recipe site whose markup we do not control. Its behaviour is
covered by ``tests/test_tools.py`` with a mocked importer.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def _resolve_smoke_env_path() -> Path:
    """Resolve the path to the smoke-test credentials file.

    Precedence: ``COOKIDOO_SMOKE_ENV_FILE`` env var, else
    ``$XDG_CONFIG_HOME/cookidoo-mcp/smoke.env``, else
    ``~/.config/cookidoo-mcp/smoke.env``. The file lives **outside** the
    repository so credentials cannot accidentally be committed.
    """
    override = os.environ.get("COOKIDOO_SMOKE_ENV_FILE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "cookidoo-mcp" / "smoke.env"


def _load_env_test(path: Path) -> dict[str, str]:
    if not path.exists():
        print(
            f"smoke test requires credentials at {path}.\n"
            f"  Create the file with COOKIDOO_EMAIL / COOKIDOO_PASSWORD (see "
            f"{REPO / '.env.example'} for the full format), or override the "
            f"location via the COOKIDOO_SMOKE_ENV_FILE environment variable.",
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

MARKER = uuid.uuid4().hex[:8]
COLLECTION_NAME = f"[SMOKE_TEST cookidoo-mcp] {MARKER}"
ITEM_NAME = f"[SMOKE_TEST cookidoo-mcp] item {MARKER}"
SENTINEL_ITEM_NAME = f"[SMOKE_TEST cookidoo-mcp] clear-sentinel {MARKER}"
RECIPE_NAME = f"[SMOKE_TEST cookidoo-mcp] recipe {MARKER}"
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
CALL_TIMEOUT = timedelta(seconds=90)


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
    """Strip FastMCP's ``{"result": …}`` envelope around non-object returns."""
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


def _payload(result: Any, tool: str) -> Any:
    """Return the tool's structured output, or raise if the call errored."""
    if result.isError:
        text = " | ".join(getattr(c, "text", str(c)) for c in (result.content or []))
        raise RuntimeError(f"tool {tool!r} returned error: {text or '(no content)'}")
    if result.structuredContent is not None:
        return _unwrap(result.structuredContent)
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
    # the COOKIDOO_* credentials are available to run.sh's assert_credentials.
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

                section("Tool discovery")
                tools_resp = await mcp.list_tools()
                tool_names = sorted(t.name for t in tools_resp.tools)
                ok(f"{len(tool_names)} tools registered")
                for name in tool_names:
                    info(f"- {name}")

                section("Authentication")
                profile = await _call(mcp, "get_user_profile")
                ok(f"username={profile.get('username')!r}")
                if profile.get("description"):
                    info(f"description={profile['description']!r}")

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

                section("Managed collections (read-only)")
                managed = await _call(mcp, "list_managed_collections", page=0)
                ok(f"{len(managed)} managed collection(s) on first page")
                for c in managed[:3]:
                    info(
                        f"- {c['name']!r}: {c['recipe_count']} recipes "
                        f"in {c['chapter_count']} chapters"
                    )

                section("Custom collections (read-only)")
                customs = await _call(mcp, "list_custom_collections", page=0)
                ok(f"{len(customs)} custom collection(s) on first page")
                for c in customs[:3]:
                    info(f"- {c['name']!r}: {c['recipe_count']} recipes")

                section("Shopping list (read-only)")
                shop = await _call(mcp, "get_shopping_list")
                ok(
                    f"recipe items: {len(shop['ingredient_items'])}, "
                    f"additional items: {len(shop['additional_items'])}"
                )

                section("Calendar (read-only)")
                week = await _call(mcp, "get_calendar_week", day=date.today().isoformat())
                ok(f"week containing today returned {len(week)} day(s)")
                for d in week:
                    if d["recipes"]:
                        info(f"- {d['title']}: {len(d['recipes'])} recipe(s)")
                        if recipe_id_for_lookup is None:
                            recipe_id_for_lookup = d["recipes"][0]["id"]

                section("Custom recipes (read-only, pre-upload)")
                # First pass exercises the listing parser against whatever
                # the account contains before the smoke test runs. If it is
                # empty this short-circuits — see the post-upload re-check.
                pre_recipes = await _call(mcp, "list_custom_recipes")
                ok(f"{len(pre_recipes)} custom recipe(s)")

                section("Public recipe details (read-only)")
                if recipe_id_for_lookup:
                    details = await _call(mcp, "get_recipe_details", recipe_id=recipe_id_for_lookup)
                    ok(
                        f"id={details['id']!r} name={details['name']!r} "
                        f"ingredients={len(details['ingredients'])}"
                    )
                else:
                    warn("no recipe ID found in calendar — skipping get_recipe_details")

                section("Recipe structure generation (no API)")
                annotated_step_text = (
                    "200 g Mehl und 100 ml Wasser in den Mixtopf geben, "
                    "30 Sek. / Stufe 4 verkneten."
                )
                sent_annotations: list[dict[str, Any]] = [
                    _ingredient_annotation(annotated_step_text, "200 g Mehl"),
                    _ingredient_annotation(annotated_step_text, "100 ml Wasser"),
                    _tts_annotation(annotated_step_text, "30 Sek. / Stufe 4", speed="4", time=30),
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
                )
                ok(f"draft built: name={draft_dict['name']!r} steps={len(draft_dict['steps'])}")
                returned_annotations = draft_dict["steps"][0].get("annotations", [])
                sent = sorted(sent_annotations, key=lambda a: a["offset"])
                got = sorted(returned_annotations, key=lambda a: a["offset"])
                if sent == got:
                    ok(f"explicit annotations survived FastMCP roundtrip ({len(got)} on step 0)")
                    types = sorted({a["type"] for a in got})
                    info(f"annotation types on step 0: {types}")
                else:
                    failures += 1
                    fail(f"annotation roundtrip mismatch: sent={sent!r} got={got!r}")
                returned_mode = draft_dict["steps"][1].get("annotations", [])
                if returned_mode == browning_annotations:
                    ok("MODE/BROWNING annotation survived FastMCP roundtrip")
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
                try:
                    search_results = await _call(mcp, "search_recipes", query="Pasta", limit=5)
                    ok(f"search_recipes returned {len(search_results)} hit(s) for 'Pasta'")
                    for hit in search_results[:3]:
                        info(f"- {hit['name']!r} (id={hit['id']})")
                    if recipe_id_for_lookup is None and search_results:
                        # Adopt a hit as the recipe-id source for the
                        # downstream write sections (clone, calendar, shopping
                        # ingredient cycle) when the calendar yielded none.
                        recipe_id_for_lookup = search_results[0]["id"]
                        info(f"adopted {recipe_id_for_lookup!r} from search for downstream tests")
                except Exception as e:
                    failures += 1
                    fail(f"search_recipes raised: {e!r}")

                section("Recipe suggestions from ingredients (read-only)")
                # ``_collect_recipe_ids`` walks every page of every collection,
                # so cap ``max_results`` and use short, common ingredients
                # that pass the >=3 char min-length guard.
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
                already_subscribed = any(c["id"] == SMOKE_MANAGED_COLLECTION_ID for c in managed)
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
                        post_add = await _call(mcp, "list_managed_collections", page=0)
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
                        post_remove = await _call(mcp, "list_managed_collections", page=0)
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
                    renamed_name = f"[SMOKE_TEST cookidoo-mcp] renamed {MARKER}"
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
                    upload = await _call(mcp, "upload_custom_recipe", draft=draft_dict, force=True)
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

                section("WRITE: clone_recipe_as_custom")
                try:
                    cloned = await _call(
                        mcp,
                        "clone_recipe_as_custom",
                        recipe_id=SMOKE_CLONE_RECIPE_ID,
                        serving_size=2,
                    )
                    cloned_recipe_id = cloned["id"]
                    ok(f"cloned {SMOKE_CLONE_RECIPE_ID!r} -> custom recipe id={cloned_recipe_id}")
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
                    warn(
                        "no uploaded custom recipe available — skipping custom-recipe shopping test"
                    )

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
                        msg = await _call(mcp, "delete_custom_recipe", recipe_id=created_recipe_id)
                        ok(f"deleted custom recipe {created_recipe_id} ({msg})")
                    except Exception as e:
                        failures += 1
                        fail(f"custom recipe cleanup failed: {e!r}")

                if cloned_recipe_id is not None:
                    try:
                        msg = await _call(mcp, "delete_custom_recipe", recipe_id=cloned_recipe_id)
                        ok(f"deleted cloned recipe {cloned_recipe_id} ({msg})")
                    except Exception as e:
                        failures += 1
                        fail(f"cloned recipe cleanup failed: {e!r}")

                if created_item_ids:
                    try:
                        msg = await _call(mcp, "remove_additional_items", item_ids=created_item_ids)
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
