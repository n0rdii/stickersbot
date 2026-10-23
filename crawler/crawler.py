"""Collects sticker packs from a public Telegram channel into base-stickerpacks.txt."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("crawler")

TG_PAGE = re.compile(r"data-post=\"[^\"]+?(\d+)\"")   # the data-post="@channel/123" attribute
PACK_RE = re.compile(r"(?:t|telegram)\.me/addstickers/([A-Za-z0-9_]+)")
UA = {"User-Agent": "Mozilla/5.0 (stickerpacks-crawler; +https://core.telegram.org)"}
SESSION = ROOT / "crawler" / "tgsession"             # telethon appends ".session"

MTPROTO_HOWTO = (
    "MTProto fallback skipped: it needs a user session (bots cannot read channel history).\n"
    "  1. install telethon: uv add telethon (or pip install telethon)\n"
    "  2. put TG_API_ID / TG_API_HASH (from https://my.telegram.org) into parser/.env\n"
    "  3. run this command once in a real console: enter the phone + login code,\n"
    "     the session is saved to crawler/tgsession.session (or TG_SESSION=<StringSession>)\n"
    "  then rerun the same command (or add --mtproto to force the mode)"
)


def fetch(url: str, timeout: float = 15.0, tries: int = 3) -> str:
    last = "?"
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = repr(e)
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt + 1, tries, last)
            time.sleep(2 ** attempt)
    raise SystemExit(f"failed to fetch page {url}: {last}")


def page_packs(html: str) -> tuple[list[str], int | None]:
    posts = [int(m) for m in TG_PAGE.findall(html)]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in PACK_RE.findall(html):
        if name.lower() not in seen:
            seen.add(name.lower())
            ordered.append(name)
    return ordered, (min(posts) if posts else None)


def known_packs(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.search(r"addstickers/(\w+)", line) or re.fullmatch(r"@?(\w+)", line)
        if m:
            out.add(m.group(1).lower())
    return out


def normalize_channel(arg: str) -> str:
    a = arg.strip().split("?")[0].split("#")[0]
    if a.startswith("@"):
        a = a[1:]
    m = re.fullmatch(r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?([A-Za-z0-9_]+)", a)
    if not (m or re.fullmatch(r"\w+", a)):
        raise SystemExit(f"cannot parse the channel address: {arg}")
    return m.group(1) if m else a


def crawl(channel: str, max_pages: int, pause: float) -> tuple[list[str], bool]:
    base = f"https://t.me/s/{channel}"
    pages: list[list[str]] = []
    min_seen: int | None = None
    posts_seen = False
    for n in range(max_pages):
        url = base if min_seen is None else f"{base}?before={min_seen}"
        packs, cur_min = page_packs(fetch(url))
        log.debug("page %s: %d pack links, min post %s", url, len(packs), cur_min)
        if packs:
            pages.append(packs)
        if n == 0 and cur_min is None:
            log.warning("the web preview %s has no posts: the channel is private, deleted, "
                        "or not a channel at all (user/bot)", url)
        if cur_min is None or (min_seen is not None and cur_min >= min_seen):
            break                                        # no older posts left
        posts_seen = True
        min_seen = cur_min
        if n + 1 < max_pages:
            time.sleep(pause)
    else:
        log.info("stopped after max-pages=%d pages", max_pages)
    out: list[str] = []
    seen: set[str] = set()
    for page in reversed(pages):                         # oldest posts -> newest
        for name in page:
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out, posts_seen


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for cand in (ROOT / "parser" / ".env", ROOT / "crawler" / ".env"):
        if cand.exists():
            load_dotenv(cand, override=False)


def tg_credentials() -> tuple[int, str]:
    load_env()
    api_id = int(os.getenv("TG_API_ID") or 0)
    api_hash = os.getenv("TG_API_HASH") or ""
    if not api_id or not api_hash:
        raise SystemExit("TG_API_ID / TG_API_HASH are not set:\n" + MTPROTO_HOWTO)
    return api_id, api_hash


def mtproto_available() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    try:
        tg_credentials()
    except SystemExit:
        return False
    return True


def _button_urls(msg) -> list[str]:
    return [b.url for row in (msg.buttons or []) for b in row if getattr(b, "url", None)]


async def crawl_mtproto_async(channel: str, max_posts: int, pause: float,
                              resolve_pause: float = 0.1, resolve_parallel: int = 4) -> list[str]:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import GetStickerSetRequest
    from telethon.tl.types import (DocumentAttributeSticker, InputStickerSetID,
                                   InputStickerSetShortName)

    api_id, api_hash = tg_credentials()
    if os.getenv("TG_SESSION"):
        client = TelegramClient(StringSession(os.getenv("TG_SESSION")), api_id, api_hash)
    else:
        client = TelegramClient(str(SESSION), api_id, api_hash)

    ordered: list = []                    # str names and ("set", set_id) placeholders
    unresolved: dict[int, tuple[int, int]] = {}   # set_id -> (id, access_hash)
    resolved: dict[int, str] = {}                 # set_id -> short_name

    async def resolve(sid: int, shash: int) -> str:
        for attempt in (1, 2, 3):
            try:
                res = await client(GetStickerSetRequest(
                    stickerset=InputStickerSetID(id=sid, access_hash=shash), hash=0))
                return res.set.short_name
            except FloodWaitError as e:
                if attempt == 3:
                    raise
                log.warning("flood limit on GetStickerSet, waiting %ds", e.seconds)
                await asyncio.sleep(e.seconds + 1)

    await client.connect()
    try:
        if not await client.is_user_authorized():
            log.info("the session is not authorized: starting the interactive login (phone + code)")
            try:
                await client.start()
            except EOFError:
                raise SystemExit("interactive login needs a real terminal (phone + code): "
                                 "run the command once in your own console")
        n_posts = 0
        async for msg in client.iter_messages(channel, limit=max_posts or None):
            n_posts += 1
            if n_posts % 1000 == 0:
                log.info("... %d posts read, %d packs so far", n_posts, len(ordered))
            text = msg.raw_text or ""
            for name in PACK_RE.findall(text) + [n for u in _button_urls(msg)
                                                 for n in PACK_RE.findall(u)]:
                ordered.append(name)
            doc = msg.sticker
            if doc is not None:
                for attr in doc.attributes:
                    if not (isinstance(attr, DocumentAttributeSticker) and attr.stickerset):
                        continue
                    ss = attr.stickerset
                    if isinstance(ss, InputStickerSetShortName):
                        ordered.append(ss.short_name)
                    elif isinstance(ss, InputStickerSetID) and ss.id not in resolved:
                        if ss.id not in unresolved:
                            unresolved[ss.id] = (ss.id, ss.access_hash)
                            ordered.append(("set", ss.id))       # keep the history position
        log.info("posts read: %d, unique sticker sets to resolve: %d", n_posts, len(unresolved))
        sem = asyncio.Semaphore(max(1, resolve_parallel))
        done = 0

        async def resolve_one(sid: int) -> None:
            nonlocal done
            async with sem:
                try:
                    resolved[sid] = await resolve(*unresolved[sid])
                    log.debug("set %d resolved -> %s", sid, resolved[sid])
                except Exception as e:                # deleted set etc. - do not lose the rest
                    log.warning("cannot resolve sticker set %d: %r", sid, e)
                if resolve_pause:
                    await asyncio.sleep(resolve_pause)
                done += 1
                if done % 300 == 0:
                    log.info("... resolved %d/%d sets", done, len(unresolved))

        await asyncio.gather(*(resolve_one(sid) for sid in list(unresolved)))
    finally:
        await client.disconnect()
    out: list[str] = []                               # oldest posts -> newest, like the web mode
    seen: set[str] = set()
    for item in reversed(ordered):
        name = item if isinstance(item, str) else resolved.get(item[1], "")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def crawl_mtproto(channel: str, max_posts: int, pause: float,
                  resolve_pause: float = 0.1, resolve_parallel: int = 4) -> list[str]:
    try:
        import telethon  # noqa: F401
    except ImportError:
        raise SystemExit("the MTProto mode needs telethon:\n" + MTPROTO_HOWTO)
    return asyncio.run(crawl_mtproto_async(channel, max_posts, pause,
                                           resolve_pause, resolve_parallel))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channel", help="channel: @name, t.me/name or https://t.me/s/name")
    ap.add_argument("--file", default=str(ROOT / "parser" / "base-stickerpacks.txt"))
    ap.add_argument("--max-pages", type=int, default=0, help="page limit (0 = until the end of the history)")
    ap.add_argument("--pause", type=float, default=0.5, help="pause between pages / set resolutions, seconds")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--mtproto", action="store_true",
                    help="read the channel with a user session (telethon): needed when the posts "
                         "carry the stickers themselves instead of t.me/addstickers/ links")
    ap.add_argument("--max-posts", type=int, default=0,
                    help="MTProto mode: message limit (0 = the whole history)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--resolve-pause", type=float, default=0.1,
                    help="MTProto mode: pause between GetStickerSetRequest calls, seconds")
    ap.add_argument("--resolve-parallel", type=int, default=4,
                    help="MTProto mode: how many GetStickerSetRequest run concurrently")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    path = Path(args.file)
    have = known_packs(path)
    log.info("%s currently holds %d packs", path, len(have))

    channel = normalize_channel(args.channel)
    if args.mtproto:
        log.info("scanning @%s via MTProto (user session, resolve_parallel=%d)",
                 channel, args.resolve_parallel)
        packs = crawl_mtproto(channel, args.max_posts, args.pause,
                              args.resolve_pause, args.resolve_parallel)
    else:
        log.info("scanning https://t.me/s/%s", channel)
        packs, posts_seen = crawl(channel, args.max_pages or 2 ** 31, args.pause)
        if not packs and posts_seen:
            log.info("the web preview shows no pack links: the posts likely carry the stickers "
                     "themselves -> trying the MTProto fallback")
            if mtproto_available():
                packs = crawl_mtproto(channel, args.max_posts, args.pause,
                                      args.resolve_pause, args.resolve_parallel)
            else:
                log.warning("%s", MTPROTO_HOWTO)
    new = [p for p in packs if p.lower() not in have]
    log.info("packs found in total: %d (unique case-insensitively: %d)",
             len(packs), len({p.lower() for p in packs}))
    log.info("new for the DB: %d", len(new))
    for p in new:
        log.info("  + %s", p)

    if not new:
        log.info("the file is unchanged"); return
    if args.dry_run:
        log.info("dry-run: nothing written"); return
    head = path.read_text(encoding="utf-8") if path.exists() else ""
    tail = "" if (not head or head.endswith("\n")) else "\n"
    lines = [f"https://t.me/addstickers/{p}" for p in new]
    path.write_text(head + tail + "\n".join(lines) + "\n", encoding="utf-8")
    log.info("appended %d pack links to %s", len(lines), path)


if __name__ == "__main__":
    main()
