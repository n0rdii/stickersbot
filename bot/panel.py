"""Admin panel: aiohttp server in a bot thread + single-page JS frontend."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

COOKIE = "panel_token"
DIR = Path(__file__).parent
DAY = 86400
PACK_RE = re.compile(r"[A-Za-z0-9_]{3,64}")
LOGIN_MAX_FAILS = 5            # wrong passwords from one IP ...
LOGIN_WINDOW = 300             # ... within this many seconds lock that IP for the rest of it


def start_panel(engine, store, host: str, port: int, password: str) -> threading.Thread | None:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return None
    t = threading.Thread(target=_serve, args=(engine, store, host, port, password),
                         name="admin-panel", daemon=True)
    t.start()
    return t


def _serve(engine, store, host: str, port: int, password: str) -> None:
    from aiohttp import web

    app = web.Application()
    app["engine"], app["store"] = engine, store
    app["sessions"] = set()        # tokens currently logged in (lost on restart - fine)
    fails: dict[str, list[float]] = {}   # ip -> monotonic times of recent failed logins
    static_html = (DIR / "panel.html").read_text(encoding="utf-8")
    static_js = (DIR / "panel.js").read_text(encoding="utf-8")

    @web.middleware
    async def auth_middleware(request, handler):
        if request.path.startswith("/api") and request.path not in (
                "/api/login", "/api/logout", "/api/me"):
            got = request.cookies.get(COOKIE, "")
            if not got or got not in app["sessions"]:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    # ------------------------------------------------------------------ static
    async def home(_request):
        return web.Response(text=static_html, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    async def panel_js(_request):
        return web.Response(text=static_js, content_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    # -------------------------------------------------------------------- auth
    async def login(request):
        data = await _json_body(request)
        if data is None or not isinstance(data.get("password"), str):
            return web.json_response({"error": "password required"}, status=400)
        ip = request.remote or "?"
        now = time.monotonic()
        recent = [t for t in fails.get(ip, []) if now - t < LOGIN_WINDOW]
        if len(recent) >= LOGIN_MAX_FAILS:         # locked: even the right password waits
            wait = int(LOGIN_WINDOW - (now - recent[0])) + 1
            return web.json_response({"error": f"too many attempts, retry in {wait}s"},
                                     status=429, headers={"Retry-After": str(wait)})
        if not hmac.compare_digest(data["password"].encode(), password.encode()):
            await asyncio.sleep(0.3)
            fails[ip] = recent + [now]
            if len(fails) > 1000:                  # keep the table bounded
                for k in [k for k, v in fails.items() if now - v[-1] >= LOGIN_WINDOW]:
                    del fails[k]
            return web.json_response({"error": "wrong password"}, status=401)
        fails.pop(ip, None)
        sess = secrets.token_hex(32)
        app["sessions"].add(sess)
        resp = web.json_response({"authed": True})
        resp.set_cookie(COOKIE, sess, httponly=True, samesite="Strict",
                        max_age=30 * DAY, path="/")
        return resp

    async def logout(_request):
        app["sessions"].discard(_request.cookies.get(COOKIE, ""))
        resp = web.json_response({"authed": False})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def me(request):
        authed = request.cookies.get(COOKIE, "") in app["sessions"]
        return web.json_response({"authed": bool(authed)})

    # ---------------------------------------------------------------- dashboard
    async def stats(_request):
        s = await asyncio.to_thread(app["store"].stats)
        return web.json_response(s)

    async def queries(request):
        try:
            page = int(request.query.get("page", "1"))
            size = int(request.query.get("page_size", "50"))
        except ValueError:
            return web.json_response({"error": "bad pagination parameters"}, status=400)
        return web.json_response(
            await asyncio.to_thread(app["store"].search_history, page, size))

    # ---------------------------------------------------------------- stickers
    async def search(request):
        q = (request.query.get("q") or "").strip()
        if len(q) < 2:
            return web.json_response({"error": "query: at least 2 characters"}, status=400)
        results = await asyncio.to_thread(app["engine"].search, q, 0)
        out = []
        for r in results:
            full = await asyncio.to_thread(app["engine"].get_sticker, r["sid"]) or {}
            out.append({**r, "description": full.get("description") or ""})
        return web.json_response(out)

    async def save_description(request):
        try:
            sid = int(request.match_info["sid"])
        except ValueError:
            return web.json_response({"error": "bad sticker id"}, status=400)
        data = await _json_body(request)
        text = " ".join(str(data.get("description", "")).split())
        try:
            old = await asyncio.to_thread(app["engine"].apply_description, sid, text, "panel")
            await asyncio.to_thread(app["store"].record_edit,
                                    sid, "panel", old, text[:1000])
            return web.json_response({"ok": True})
        except KeyError as e:
            return web.json_response({"error": str(e)}, status=404)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    # -------------------------------------------------------------- suggestions
    async def suggestions(_request):
        rows = await asyncio.to_thread(app["store"].pending_suggestions)
        out = []
        for s in rows:
            cur = await asyncio.to_thread(app["engine"].get_sticker, s["sid"])
            out.append({**s, "current": cur["description"] if cur else None})
        return web.json_response(out)

    async def decide_suggestion(request):
        try:
            sug_id = int(request.match_info["id"])
        except ValueError:
            return web.json_response({"error": "bad suggestion id"}, status=400)
        data = await _json_body(request)
        if data is None or not isinstance(data.get("approve"), bool):
            return web.json_response({"error": "approve: boolean required"}, status=400)
        approved = data["approve"]
        row = next((s for s in await asyncio.to_thread(app["store"].pending_suggestions)
                    if s["id"] == sug_id), None)
        if row is None:
            return web.json_response({"error": "suggestion not found"}, status=404)
        if approved:
            cur = await asyncio.to_thread(app["engine"].get_sticker, row["sid"])
            if cur is None:
                await asyncio.to_thread(app["store"].decide_suggestion, sug_id, False)
                return web.json_response({"ok": False, "note": "sticker is gone - rejected"})
            try:
                old = await asyncio.to_thread(app["engine"].apply_description,
                                              row["sid"], row["text"],
                                              f"user:{row['user_id']}")
                await asyncio.to_thread(app["store"].record_edit, row["sid"],
                                        f"user:{row['user_id']}", old, row["text"])
                await asyncio.to_thread(app["store"].decide_suggestion, sug_id, True)
                return web.json_response({"ok": True})
            except KeyError as e:
                return web.json_response({"error": str(e)}, status=404)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
        await asyncio.to_thread(app["store"].decide_suggestion, sug_id, False)
        return web.json_response({"ok": True})

    # ------------------------------------------------------- global pack boosts
    async def boosts(_request):
        names = await asyncio.to_thread(app["engine"].set_names)
        fold = {n.casefold() for n in names}
        rows = await asyncio.to_thread(app["store"].pack_boosts)
        return web.json_response(
            [{**r, "in_index": r["pack"].casefold() in fold} for r in rows])

    async def add_boost(request):
        data = await _json_body(request)
        if data is None:
            return web.json_response({"error": "JSON body expected"}, status=400)
        pack = str(data.get("pack") or "").strip()
        if not PACK_RE.fullmatch(pack):
            return web.json_response(
                {"error": "pack: expected a set short name ([A-Za-z0-9_]{3,64})"}, status=400)
        try:
            weight = float(data.get("weight") if data.get("weight") is not None else 2.0)
        except (TypeError, ValueError):
            return web.json_response({"error": "weight: number required"}, status=400)
        if not 1.0 < weight <= 50.0:
            return web.json_response(
                {"error": "weight must be in the range (1, 50]"}, status=400)
        note = " ".join(str(data.get("note") or "").split())[:64]
        await asyncio.to_thread(app["store"].upsert_boost, pack, weight, note)
        return web.json_response({"ok": True})

    async def delete_boost(request):
        pack = (request.match_info["pack"] or "").strip()
        if not await asyncio.to_thread(app["store"].remove_boost, pack):
            return web.json_response({"error": "boost not found"}, status=404)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------- pack requests
    async def pack_requests(_request):
        names = await asyncio.to_thread(app["engine"].set_names)
        fold = {n.casefold() for n in names}
        rows = await asyncio.to_thread(app["store"].pack_requests)
        return web.json_response(
            [{**r, "in_index": r["pack"].casefold() in fold} for r in rows])

    async def decide_pack_request(request):
        pack = request.match_info["pack"]
        if not PACK_RE.fullmatch(pack):
            return web.json_response({"error": "bad pack name"}, status=400)
        data = await _json_body(request)
        if data is None or not isinstance(data.get("approve"), bool):
            return web.json_response({"error": "approve: boolean required"}, status=400)
        if not await asyncio.to_thread(app["store"].decide_pack_request, pack, data["approve"]):
            return web.json_response({"error": "request not found"}, status=404)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ helpers
    async def _json_body(request):
        try:
            data = json.loads((await request.text()) or "{}")
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    app.router.add_get("/", home)
    app.router.add_get("/panel.js", panel_js)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/queries", queries)
    app.router.add_get("/api/search", search)
    app.router.add_post("/api/sticker/{sid}", save_description)
    app.router.add_get("/api/suggestions", suggestions)
    app.router.add_post("/api/suggestion/{id}", decide_suggestion)
    app.router.add_get("/api/pack_requests", pack_requests)
    app.router.add_post("/api/pack_request/{pack}", decide_pack_request)
    app.router.add_get("/api/boosts", boosts)
    app.router.add_post("/api/boosts", add_boost)
    app.router.add_delete("/api/boosts/{pack}", delete_boost)

    web.run_app(app, host=host, port=port, access_log=None, handle_signals=False,
                print=lambda *a, **k: None)
