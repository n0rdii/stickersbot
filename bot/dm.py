"""Private-chat side of the bot: favorite packs, adding packs, sticker cards."""
from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import texts as T
from store import MAX_PREFS, pack_key

log = logging.getLogger("bot.dm")

dm_router = Router()

PACKS_PAGE = 8                   # rows per page of the favorites / popular lists
MODE_TTL = 900                   # seconds a "send me the pack / the text" prompt stays armed
MAX_PACK_REQUESTS_PER_DAY = 10   # new packs one user may ask for per 24h (silent limit)
PACK_NAME_RE = re.compile(r"[A-Za-z0-9_]{3,64}")
LINK_RE = re.compile(r"(?:t|telegram)\.me/addstickers/([A-Za-z0-9_]{3,64})", re.IGNORECASE)

_modes: dict[int, tuple[str, object, float]] = {}
_cards: dict[int, dict] = {}
_bot_names: dict[int, str] = {}


def _esc(value) -> str:
    return html.escape(str(value), quote=False)


def _prune() -> None:
    cutoff = time.time() - MODE_TTL
    for d, idx in ((_modes, 2), (_cards, None)):
        if len(d) > 2000:
            for k in [k for k, v in d.items() if (v[idx] if idx else v["ts"]) < cutoff]:
                d.pop(k, None)


def _set_mode(uid: int, kind: str, payload=None) -> None:
    _prune()
    _modes[uid] = (kind, payload, time.time())


def _get_mode(uid: int):
    m = _modes.get(uid)
    if m and time.time() - m[2] > MODE_TTL:
        _modes.pop(uid, None)
        return None
    return m


async def _bot_name(bot: Bot) -> str:
    if bot.id not in _bot_names:
        try:
            _bot_names[bot.id] = (await bot.me()).username or "this_bot"
        except TelegramAPIError:
            return "this_bot"
    return _bot_names[bot.id]


async def _say(msg: Message, text: str, kb: InlineKeyboardBuilder | None = None):
    await msg.answer(text, parse_mode=ParseMode.HTML,
                     reply_markup=kb.as_markup() if kb else None)


async def _edit(cb: CallbackQuery, text: str, kb: InlineKeyboardBuilder | None = None):
    if cb.message is None:
        return
    try:
        await cb.message.edit_text(text, parse_mode=ParseMode.HTML,
                                   reply_markup=kb.as_markup() if kb else None)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.info("edit failed: %s", e)


# ------------------------------------------------------------------ keyboards / views
def main_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(Btn(text=T.BTN_MY_PACKS, callback_data="m:fav:0"),
           Btn(text=T.BTN_ADD, callback_data="m:add"))
    return kb


def _nav(kb: InlineKeyboardBuilder, kind: str, page: int, has_prev: bool, has_next: bool):
    row = []
    if has_prev:
        row.append(Btn(text="«", callback_data=f"m:{kind}:{page - 1}"))
    if has_next:
        row.append(Btn(text="»", callback_data=f"m:{kind}:{page + 1}"))
    if row:
        kb.row(*row)


async def fav_view(store, uid: int, page: int):
    prefs = await asyncio.to_thread(store.list_prefs, uid)
    pages = max(1, math.ceil(len(prefs) / PACKS_PAGE))
    page = min(max(0, page), pages - 1)
    chunk = prefs[page * PACKS_PAGE:(page + 1) * PACKS_PAGE]
    kb = InlineKeyboardBuilder()
    for name in chunk:
        kb.row(Btn(text=f"🐾 {name}", url=f"https://t.me/addstickers/{name}"),
               Btn(text=T.BTN_REMOVE, callback_data=f"unfav:{page}:{pack_key(name)}"))
    _nav(kb, "fav", page, page > 0, page + 1 < pages)
    kb.row(Btn(text=T.BTN_ADD, callback_data="m:add"),
           Btn(text=T.BTN_POPULAR, callback_data="m:pop:0"))
    text = T.FAV_HEAD + (T.FAV_FOOT if chunk else T.FAV_EMPTY)
    return text, kb


async def popular_view(store, uid: int, page: int):
    page = max(0, page)
    names, more = await asyncio.to_thread(store.popular_packs, uid, page * PACKS_PAGE,
                                          PACKS_PAGE)
    if not names and page > 0:                       # the last page emptied out
        return await popular_view(store, uid, page - 1)
    kb = InlineKeyboardBuilder()
    for name in names:
        kb.row(Btn(text=f"➕ {name}", callback_data=f"fav:{page}:{pack_key(name)}"),
               Btn(text=T.BTN_OPEN, url=f"https://t.me/addstickers/{name}"))
    _nav(kb, "pop", page, page > 0, more)
    kb.row(Btn(text=T.BTN_BACK, callback_data="m:fav:0"))
    return T.POPULAR_HEAD + ("" if names else T.POPULAR_EMPTY), kb


def card_kb(is_fav: bool, pack_known: bool, sid: int | None) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if pack_known:
        kb.row(Btn(text=T.BTN_CARD_UNFAV if is_fav else T.BTN_CARD_FAV,
                   callback_data="card:unfav" if is_fav else "card:fav"))
    else:
        kb.row(Btn(text=T.BTN_CARD_ADD, callback_data="card:add"))
    if sid is not None:
        kb.row(Btn(text=T.BTN_CARD_EDIT, callback_data=f"card:edit:{sid}"))
    kb.row(Btn(text=T.BTN_MY_PACKS, callback_data="m:fav:0"))
    return kb


# ------------------------------------------------------------------ adding a pack
async def add_pack(bot: Bot, store, engine, uid: int, name: str,
                   auto_approve: bool = False) -> dict:
    info = await asyncio.to_thread(engine.pack_info, name)
    if info:                                         # already searchable: just favorite it
        ok = await asyncio.to_thread(store.add_pref, uid, info["name"])
        return {"status": "indexed", "title": info["title"], "count": info["count"],
                "fav_full": not ok}
    if await asyncio.to_thread(store.pack_requests_today, uid) >= MAX_PACK_REQUESTS_PER_DAY:
        return {"status": "limit"}
    try:
        s = await bot.get_sticker_set(name)
    except TelegramBadRequest:
        return {"status": "not_found"}
    except TelegramAPIError:
        log.exception("get_sticker_set(%s) failed", name)
        return {"status": "tg_error"}
    if getattr(s, "sticker_type", "regular") != "regular":
        return {"status": "not_stickers"}
    title, count = s.title or s.name, len(s.stickers)
    await asyncio.to_thread(store.add_pack_request, uid, s.name, title, count, auto_approve)
    ok = await asyncio.to_thread(store.add_pref, uid, s.name)
    return {"status": "queued", "title": title, "count": count, "fav_full": not ok}


def add_result_text(res: dict) -> str:
    st = res["status"]
    if st == "indexed":
        text = T.PACK_ALREADY_THERE.format(title=_esc(res["title"]))
    elif st == "queued":
        text = T.PACK_ADDED.format(title=_esc(res["title"]), count=res["count"])
    else:
        return {"not_found": T.PACK_NOT_FOUND, "not_stickers": T.PACK_NOT_STICKERS,
                "limit": T.PACK_LIMIT, "tg_error": T.PACK_TG_ERROR}[st]
    if res.get("fav_full"):
        text += "\n\n" + T.FAV_FULL.format(n=MAX_PREFS)
    return text


async def _do_add(msg: Message, bot: Bot, store, engine, cfg, uid: int, name: str):
    res = await add_pack(bot, store, engine, uid, name,
                         auto_approve=bool(getattr(cfg, "auto_approve_packs", False)))
    ok = res["status"] in ("indexed", "queued")
    kb = InlineKeyboardBuilder()
    kb.row(Btn(text=T.BTN_MY_PACKS, callback_data="m:fav:0"),
           Btn(text=T.BTN_ADD, callback_data="m:add"))
    await _say(msg, add_result_text(res), kb if ok else None)
    return ok


# ------------------------------------------------------------------ handlers
is_private = F.chat.type == ChatType.PRIVATE


@dm_router.message(Command("start"), is_private)
@dm_router.message(Command("help"), is_private)
async def h_start(m: Message, bot: Bot):
    await _say(m, T.START.format(bot=_esc(await _bot_name(bot))), main_kb())


@dm_router.message(Command("packs"), is_private)
async def h_packs(m: Message, store):
    text, kb = await fav_view(store, m.from_user.id, 0)
    await _say(m, text, kb)


@dm_router.message(Command("cancel"), is_private)
async def h_cancel(m: Message):
    if _modes.pop(m.from_user.id, None):
        await _say(m, T.CANCELLED, main_kb())
    else:
        await _say(m, T.NOTHING_TO_CANCEL)


@dm_router.callback_query(F.data.startswith("m:"))
async def cb_menu(cb: CallbackQuery, store):
    parts = cb.data.split(":")
    try:
        if parts[1] == "fav":
            text, kb = await fav_view(store, cb.from_user.id, int(parts[2]))
            await cb.answer()
            await _edit(cb, text, kb)
        elif parts[1] == "pop":
            text, kb = await popular_view(store, cb.from_user.id, int(parts[2]))
            await cb.answer()
            await _edit(cb, text, kb)
        elif parts[1] == "add":
            _set_mode(cb.from_user.id, "add_pack")
            await cb.answer("🐾")
            if cb.message is not None:
                await _say(cb.message, T.ADD_PROMPT)
        else:
            await cb.answer(T.OLD_MENU, show_alert=True)
    except (ValueError, IndexError):
        await cb.answer(T.OLD_MENU, show_alert=True)


@dm_router.callback_query(F.data.startswith("unfav:"))
async def cb_unfav(cb: CallbackQuery, store):
    try:
        _, page_s, key = cb.data.split(":", 2)
        page = int(page_s)
    except ValueError:
        await cb.answer(T.OLD_MENU, show_alert=True)
        return
    uid = cb.from_user.id
    pack = await asyncio.to_thread(store.pack_by_key, uid, key)
    if pack is not None:
        await asyncio.to_thread(store.remove_pref, uid, pack)
        await cb.answer(T.FAV_REMOVED.format(name=pack))
    else:
        await cb.answer()                         # already gone - just redraw
    text, kb = await fav_view(store, uid, page)
    await _edit(cb, text, kb)


@dm_router.callback_query(F.data.startswith("fav:"))
async def cb_fav(cb: CallbackQuery, store):
    try:
        _, page_s, key = cb.data.split(":", 2)
        page = int(page_s)
    except ValueError:
        await cb.answer(T.OLD_MENU, show_alert=True)
        return
    uid = cb.from_user.id
    pack = await asyncio.to_thread(store.pack_by_key, uid, key)
    if pack is None:
        await cb.answer(T.OLD_MENU, show_alert=True)
        return
    if await asyncio.to_thread(store.add_pref, uid, pack):
        await cb.answer(T.FAV_ADDED)
    else:
        await cb.answer(T.FAV_FULL.format(n=MAX_PREFS), show_alert=True)
    text, kb = await popular_view(store, uid, page)
    await _edit(cb, text, kb)


@dm_router.callback_query(F.data.regexp(r"^(pref|prefh|packs|packsadd):"))
async def cb_legacy(cb: CallbackQuery):
    await cb.answer(T.OLD_MENU, show_alert=True)


@dm_router.callback_query(F.data.startswith("card:"))
async def cb_card(cb: CallbackQuery, bot: Bot, store, engine, cfg):
    uid = cb.from_user.id
    parts = cb.data.split(":")
    card = _cards.get(uid)
    if parts[1] == "edit":
        try:
            sid = int(parts[2])
        except (ValueError, IndexError):
            await cb.answer(T.OLD_MENU, show_alert=True)
            return
        info = await asyncio.to_thread(engine.get_sticker, sid)
        if info is None:
            await cb.answer(T.CARD_EXPIRED, show_alert=True)
            return
        _set_mode(uid, "edit", sid)
        await cb.answer("✏️")
        if cb.message is not None:
            await _say(cb.message, T.DESC_ASK.format(description=_esc(info["description"])))
        return
    if not card or cb.message is None:
        await cb.answer(T.CARD_EXPIRED, show_alert=True)
        return
    name = card["name"]
    if parts[1] == "add":
        await cb.answer("🐾")
        await _do_add(cb.message, bot, store, engine, cfg, uid, name)
        return
    if parts[1] == "fav":
        ok = await asyncio.to_thread(store.add_pref, uid, name)
        await cb.answer(T.FAV_ADDED if ok else T.FAV_FULL.format(n=MAX_PREFS),
                        show_alert=not ok)
    elif parts[1] == "unfav":
        await asyncio.to_thread(store.remove_pref, uid, name)
        await cb.answer(T.FAV_REMOVED.format(name=name))
    prefs = await asyncio.to_thread(store.user_prefs, uid)
    is_fav = name.casefold() in {p.casefold() for p in prefs}
    try:
        await cb.message.edit_reply_markup(
            reply_markup=card_kb(is_fav, True, card.get("sid")).as_markup())
    except TelegramBadRequest:
        pass


@dm_router.message(F.sticker, is_private)
async def h_sticker(m: Message, bot: Bot, store, engine, cfg):
    uid, st = m.from_user.id, m.sticker
    set_name = st.set_name or ""
    mode = _get_mode(uid)
    if not PACK_NAME_RE.fullmatch(set_name):
        await _say(m, T.NO_PACK)                     # (a pending prompt stays armed)
        return
    if mode and mode[0] == "add_pack":               # "send me a sticker from the pack"
        _modes.pop(uid, None)
        await _do_add(m, bot, store, engine, cfg, uid, set_name)
        return

    info = await asyncio.to_thread(engine.find_by_unique_id, st.file_unique_id)
    pack = await asyncio.to_thread(engine.pack_info, set_name)
    prefs = await asyncio.to_thread(store.user_prefs, uid)
    is_fav = set_name.casefold() in {p.casefold() for p in prefs}
    canon = pack["name"] if pack else set_name
    _prune()
    _cards[uid] = {"name": canon, "sid": info["sid"] if info else None, "ts": time.time()}
    if info:
        text = T.CARD_KNOWN.format(sid=info["sid"], pack=_esc(info["set_name"] or canon),
                                   description=_esc(info["description"] or "-"))
    elif pack:
        text = T.CARD_PACK_KNOWN.format(pack=_esc(canon))
    else:
        text = T.CARD_PACK_UNKNOWN.format(pack=_esc(set_name))
    await _say(m, text, card_kb(is_fav, bool(pack), info["sid"] if info else None))


@dm_router.message(F.text, is_private)
async def h_text(m: Message, bot: Bot, store, engine, cfg):
    uid = m.from_user.id
    text = (m.text or "").strip()
    mode = _get_mode(uid)

    if (link := LINK_RE.search(text)):               # a pack link works any time
        if mode and mode[0] == "add_pack":
            _modes.pop(uid, None)
        await _do_add(m, bot, store, engine, cfg, uid, link.group(1))
        return

    if mode and mode[0] == "add_pack":               # plain short name
        name = text.lstrip("@")
        if not PACK_NAME_RE.fullmatch(name):
            await _say(m, T.PACK_BAD_NAME)           # stay armed
            return
        _modes.pop(uid, None)
        await _do_add(m, bot, store, engine, cfg, uid, name)
        return

    if mode and mode[0] == "edit":
        desc = " ".join(text.split())
        if not (10 <= len(desc) <= 300):
            await _say(m, T.DESC_BAD)
            return
        try:
            sug_id = await asyncio.to_thread(store.add_suggestion, uid, mode[1], desc)
        except Exception:
            log.exception("failed to save a description")
            await _say(m, T.DESC_ERROR)
            return
        _modes.pop(uid, None)
        await _say(m, T.DESC_THANKS.format(sug_id=sug_id), main_kb())
        return

    await _say(m, T.FALLBACK.format(bot=_esc(await _bot_name(bot))), main_kb())
