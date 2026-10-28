"""Sticker indexing pipeline (runs on your local GPU machine)."""
import argparse
import asyncio
import base64
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from textclean import fts_create_sql, fts_values, index_description   # parser/textclean.py

log = logging.getLogger("pipeline")

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv()

LIST_KEYS = ["characters", "objects", "actions", "emotions", "visual_tags", "style_tags"]
BG_COLOR = (235, 235, 235, 255)
MAX_SIDE = 512

SCHEMA = """
CREATE TABLE IF NOT EXISTS stickers(
  id INTEGER PRIMARY KEY,
  file_unique_id TEXT UNIQUE NOT NULL,
  file_id TEXT NOT NULL,                 -- Bot API file_id for InlineQueryResultCachedSticker
  set_name TEXT, set_title TEXT, emoji TEXT, kind TEXT,
  description TEXT,
  characters TEXT, objects TEXT, actions TEXT, emotions TEXT,
  visual_tags TEXT, style_tags TEXT,     -- JSON arrays
  nsfw INTEGER DEFAULT 0,
  emb BLOB NOT NULL,                     -- float32[dim] from description, L2-normalized
  added_ts INTEGER);
CREATE TABLE IF NOT EXISTS skipped(file_unique_id TEXT PRIMARY KEY, reason TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


# ------------------------------------------------------------------ data
@dataclass
class Item:
    uid: str
    file_id: str
    src_file_id: str
    set_name: str
    set_title: str
    emoji: str
    kind: str
    image_b64: str | None = None
    raw_caption: str | None = None
    rec: dict | None = None
    reject: str | None = None


def parse_pack_names(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.search(r"addstickers/(\w+)", line) or re.fullmatch(r"@?(\w+)", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def approved_requests(path: str) -> list[str]:
    if not path or not Path(path).exists():
        return []
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT pack FROM pack_requests WHERE status='approved' "
                              "ORDER BY created_ts").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if re.fullmatch(r"\w{3,64}", r[0] or "")]


def clean_list(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    out: list[str] = []
    seen: set[str] = set()
    for x in v if isinstance(v, list) else []:
        if not isinstance(x, str):
            continue
        x = " ".join(x.split())[:80]
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out[:30]


def parse_json_record(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("no JSON in the response")
    try:
        d = json.loads(t[s:e + 1])
    except json.JSONDecodeError as ex:
        raise ValueError(f"invalid JSON: {ex}")
    if not isinstance(d, dict):
        raise ValueError("JSON is not an object")
    desc = d.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("empty description")
    rec = {"description": " ".join(desc.split())[:1500]}
    for k in LIST_KEYS:
        rec[k] = clean_list(d.get(k))
    rec["nsfw"] = d["nsfw"] if isinstance(d.get("nsfw"), bool) else None
    return rec


def classify(rec: dict) -> bool:
    return bool(rec["nsfw"])


def prepare_image(raw: bytes) -> str:
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    bg = Image.new("RGBA", im.size, BG_COLOR)
    bg.alpha_composite(im)
    out = io.BytesIO()
    bg.convert("RGB").save(out, "JPEG", quality=92)
    return base64.b64encode(out.getvalue()).decode()


# ------------------------------------------------------------------ network
class QuotaExhausted(Exception):
    """OpenRouter is out of credits (HTTP 402): not retryable, stops the whole run."""


class TgSource:

    def __init__(self, token: str):
        from aiogram import Bot
        self.bot = Bot(token)

    async def get_pack(self, name: str):
        from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
        for attempt in range(4):
            try:
                return await self.bot.get_sticker_set(name)
            except TelegramBadRequest:
                return None
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramNetworkError:
                await asyncio.sleep(2 ** attempt)
        return None

    async def download(self, file_id: str) -> bytes:
        from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
        for attempt in range(4):
            buf = io.BytesIO()
            try:
                await self.bot.download(file_id, destination=buf)
                return buf.getvalue()
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramNetworkError:
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError("failed to download the file")

    async def close(self):
        await self.bot.session.close()


def make_session():
    import aiohttp
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))


async def post_json(session, url: str, payload: dict, headers: dict | None = None, tries: int = 4):
    import aiohttp
    last = "?"
    for attempt in range(tries):
        try:
            async with session.post(url, json=payload, headers=headers) as r:
                if r.status == 402 and "openrouter" in url:
                    raise QuotaExhausted(f"{url} returned HTTP 402 (payment required)")
                if r.status in (421, 429, 500, 502, 503, 504):
                    last = f"HTTP {r.status}"
                    try:
                        wait = float(r.headers.get("Retry-After", 0))
                    except ValueError:
                        wait = 0
                    await asyncio.sleep(max(wait, 2 ** attempt))
                    continue
                if r.status >= 400:
                    raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:300]}")
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = repr(e)
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"request failed: {last}")


async def joy_caption(session, cfg, joy_prompt: str, image_b64: str, uid: str = "") -> str:
    payload = {
        "model": cfg.joy_model,
        "messages": [
            {"role": "system", "content": cfg.joy_system},
            {"role": "user", "content": [
                {"type": "text", "text": joy_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]},
        ],
        "max_tokens": cfg.joy_max_tokens,
        "temperature": 0.0,      # recommended by the JoyCaption authors
        "top_p": 0.9,
    }
    resp = await post_json(session, cfg.joy_url, payload)
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    if len(text) < 10:
        log.warning("[joycaption] %s: short/empty answer (%d chars): %.300r",
                    uid, len(text), text)
        raise ValueError("JoyCaption returned an empty answer")
    return text


async def postprocess(session, cfg, pp_prompt: str, caption: str, uid: str = "") -> dict:
    if "{caption}" in pp_prompt:       # support prompt templates with a placeholder
        messages = [{"role": "user", "content": pp_prompt.replace("{caption}", caption)}]
    else:
        messages = [{"role": "system", "content": pp_prompt}, {"role": "user", "content": caption}]
    payload = {"model": cfg.or_model, "messages": messages, "temperature": 0.1,
               "max_tokens": cfg.pp_max_tokens, "response_format": {"type": "json_object"}}
    pp_url = cfg.pp_url
    pp_headers = None
    if "openrouter" in pp_url:
        if cfg.pp_reasoning == "off":
            payload["reasoning"] = {"enabled": False}   # otherwise it eats the whole max_tokens
        pp_headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                      "X-Title": "sticker-search-pipeline"}
    last: Exception | None = None
    for attempt in range(3):
        resp = await post_json(session, pp_url, payload, pp_headers)
        try:
            choice = resp["choices"][0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
        except (KeyError, IndexError, TypeError):
            choice, msg, content = {}, None, None
        try:
            return parse_json_record(content or "")
        except (ValueError, KeyError, IndexError) as e:
            last = e
            if content is None:                # non-standard response structure
                log.warning("[postprocess] %s: attempt %d/3: odd response shape (%s): %.600r",
                            uid, attempt + 1, e, resp)
            elif not content:                  # empty content: usually reasoning ate the limit
                reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
                log.warning("[postprocess] %s: attempt %d/3: EMPTY content "
                            "(finish_reason=%s, reasoning=%d chars: %.300r)",
                            uid, attempt + 1, choice.get("finish_reason") or "?",
                            len(reasoning), reasoning[:300])
            else:                              # content exists but didn't compress to JSON
                log.warning("[postprocess] %s: attempt %d/3 without valid JSON (%s); "
                            "model answer: %.600r", uid, attempt + 1, e, content)
            payload["temperature"] = 0.3
    raise ValueError(f"postprocessing produced no valid JSON: {last}")


# ------------------------------------------------------------------ database
def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)   # default journal: one file, rsync-friendly
    cols = {r[1] for r in db.execute("PRAGMA table_info(stickers)")}
    if cols and "description" not in cols:
        raise SystemExit(f"{path}: old schema (from embed.py). Use a new DB file: --db new.db")
    db.executescript(SCHEMA)
    db.execute(fts_create_sql())
    return db


def set_meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
               "SET value=excluded.value", (key, str(value)))


def save_batch(db, emb, batch: list[Item], counters: dict, model_name: str):
    now = int(time.time())
    for it in batch:
        if it.reject:
            db.execute("INSERT OR REPLACE INTO skipped VALUES (?,?,?)", (it.uid, it.reject, now))
            counters["rejected"] += 1
    ok = [it for it in batch if not it.reject]
    if ok:
        # embedding input must be the same cleaned text FTS indexes
        vecs = emb.passages([index_description(it.rec["description"]) or it.rec["description"]
                             for it in ok], batch=len(ok))
        for it, v in zip(ok, vecs):
            r = it.rec
            cur = db.execute(
                """INSERT OR IGNORE INTO stickers(file_unique_id,file_id,set_name,set_title,emoji,kind,
                   description,characters,objects,actions,emotions,visual_tags,style_tags,nsfw,emb,added_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it.uid, it.file_id, it.set_name, it.set_title, it.emoji, it.kind, r["description"],
                 *[json.dumps(r[k], ensure_ascii=False) for k in LIST_KEYS],
                 int(r["nsfw"]), v.tobytes(), now))
            if cur.rowcount:
                db.execute(
                    "INSERT INTO fts_stickers(rowid,description,characters,objects,actions,emotions,"
                    "visual_tags,style_tags,emoji,set_title) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cur.lastrowid, *fts_values(r["description"], r, it.emoji, it.set_title)))
                counters["saved"] += 1
        set_meta(db, "dim", len(vecs[0]))
    set_meta(db, "model", model_name)
    set_meta(db, "version", time.strftime("%Y%m%d-%H%M%S"))
    db.commit()


# ---------------------------------------------------------------- pipeline
async def reporter(counters, t0):
    while True:
        await asyncio.sleep(30)
        mins = max((time.time() - t0) / 60, 1e-6)
        log.info("%s | saved %.1f/min", dict(counters), counters["saved"] / mins)


async def run(cfg, tg=None, emb=None):
    packs = parse_pack_names(Path(cfg.packs).read_text(encoding="utf-8"))
    have = {p.lower() for p in packs}
    requested = [n for n in approved_requests(getattr(cfg, "requests_db", ""))
                 if n.lower() not in have]
    if requested:
        log.info("%d approved pack request(s) from the bot added to the list: %s",
                 len(requested), ", ".join(requested[:10]) + (" ..." if len(requested) > 10 else ""))
        packs += requested
    joy_prompt = Path(cfg.joy_prompt).read_text(encoding="utf-8").strip()
    pp_prompt = Path(cfg.pp_prompt).read_text(encoding="utf-8").strip()
    if emb is None:
        from embedder import Embedder
        emb = Embedder(cfg.model_dir, max_len=128, threads=os.cpu_count() or 1,
                       device=cfg.emb_device or None)
    tg = tg or TgSource(os.environ["BOT_TOKEN"])

    for p in (cfg.db, cfg.raw_cache):
        Path(p).parent.mkdir(parents=True, exist_ok=True)   # data/ etc. if missing
    db = open_db(cfg.db)
    if (row := db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()) \
            and int(row[0]) != emb.dim:
        db.close()
        raise SystemExit(
            f"{cfg.db}: the DB holds embeddings of dim={row[0]}, the current model gives {emb.dim}. "
            f"Point --db to a new file: the DB is append-only and must not mix vectors of "
            f"different models.")
    known = {r[0] for r in db.execute(
        "SELECT file_unique_id FROM stickers UNION SELECT file_unique_id FROM skipped")}
    cache = sqlite3.connect(cfg.raw_cache)     # raw JoyCaption captions: avoid re-running the GPU
    cache.execute("CREATE TABLE IF NOT EXISTS raw(uid TEXT PRIMARY KEY, caption TEXT, ts INTEGER)")
    raw_cache = dict(cache.execute("SELECT uid, caption FROM raw"))
    log.info("packs in the list: %d, stickers already in DB/skipped: %d, raw captions cached: %d",
             len(packs), len(known), len(raw_cache))

    counters = {k: 0 for k in
                ("saved", "rejected", "failed", "dup", "no_thumb", "packs_bad", "captioned",
                 "dropped")}
    stop = asyncio.Event()
    t0 = time.time()

    try:
        from graph import build_pipeline
    except ImportError:
        raise SystemExit("langgraph is not installed: pip install langgraph")

    async with make_session() as session:
        graph, flush = build_pipeline(cfg, tg, session, emb, db, cache, raw_cache,
                                      packs, known, counters, stop, joy_prompt, pp_prompt)
        rep = asyncio.create_task(reporter(counters, t0))
        try:
            await graph.ainvoke({}, config={"recursion_limit": 64})
        finally:
            rep.cancel()
            await flush()                     # drain the persist buffer if anything is left
            total = db.execute("SELECT COUNT(*) FROM stickers").fetchone()[0]
            set_meta(db, "count", total)
            db.commit()
            db.close()
            cache.close()
            if hasattr(tg, "close"):
                await tg.close()
    if stop.is_set():
        log.error("run stopped early: OpenRouter returned HTTP 402 (no credits left). "
                  "Counters: %s; already saved: %d stickers. The run can be continued later - "
                  "the pipeline is append-only, done work is not repeated.",
                  counters, total)
        raise SystemExit(1)
    log.info("done in %.0f s: %s, total in DB: %d", time.time() - t0, counters, total)
    return counters


def parse_args(argv=None):
    here = Path(__file__).resolve().parent       # parser/ dir with the input files
    root = here.parent                           # repository root: ./data for the DBs
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packs", default=str(here / "base-stickerpacks.txt"))
    ap.add_argument("--db", default=str(root / "data" / "stickers.db"))
    ap.add_argument("--requests-db", default=str(root / "data" / "bot_data.db"),
                    help="bot_data.db: packs users asked for and the owner approved in the "
                         "panel are indexed too ('' = ignore requests)")
    ap.add_argument("--joy-prompt", default=str(here / "joycaption.prompt"))
    ap.add_argument("--pp-prompt", default=str(here / "postprocessing.prompt"))
    ap.add_argument("--model-dir",
                    default=os.getenv("MODEL_DIR") or str(here / "model_multilingual_minilm"))
    ap.add_argument("--emb-device", default=os.getenv("EMB_DEVICE", ""),
                    help="cuda/cpu for the embedder; empty = auto (cpu if torch has no CUDA)")
    ap.add_argument("--raw-cache", default=str(root / "data" / "raw_captions.db"))
    ap.add_argument("--limit", type=int, default=0, help="at most N new stickers (for a trial run)")
    ap.add_argument("--joy-url", default=os.getenv("JOY_URL", "http://127.0.0.1:8080/v1/chat/completions"))
    ap.add_argument("--joy-model", default=os.getenv("JOY_MODEL", "joycaption"),
                    help="model ID at the JoyCaption server (GET /v1/models)")
    ap.add_argument("--joy-system", default=os.getenv("JOY_SYSTEM", "You are a helpful image captioner."))
    ap.add_argument("--joy-max-tokens", type=int, default=int(os.getenv("JOY_MAX_TOKENS", "384")))
    ap.add_argument("--joy-concurrency", type=int, default=int(os.getenv("JOY_CONCURRENCY", "2")))
    ap.add_argument("--pp-concurrency", type=int, default=int(os.getenv("PP_CONCURRENCY", "6")))
    ap.add_argument("--dl-concurrency", type=int, default=int(os.getenv("DL_CONCURRENCY", "6")))
    ap.add_argument("--emb-batch", type=int, default=16)
    ap.add_argument("--or-model", default=os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash"))
    ap.add_argument("--pp-max-tokens", type=int,
                    default=int(os.getenv("PP_MAX_TOKENS", "1200")),
                    help="postprocessor generation ceiling (reasoning models spend part of it on reasoning)")
    ap.add_argument("--pp-reasoning", default=os.getenv("PP_REASONING", ""),
                    help="off = disable reasoning in the postprocessor (otherwise it may eat the whole token budget)")
    ap.add_argument("--pp-url", default=os.getenv("PP_URL", "https://openrouter.ai/api/v1/chat/completions"),
                    help="postprocessor chat-completions endpoint; point it at Ollama "
                         "(http://127.0.0.1:11434/v1/chat/completions) to use a local model")
    return ap.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = parse_args()
    needs_or_key = "openrouter" in cfg.pp_url
    if not os.getenv("BOT_TOKEN") or (needs_or_key and not os.getenv("OPENROUTER_API_KEY")):
        sys.exit("environment variable BOT_TOKEN or OPENROUTER_API_KEY is not set "
                 "(put it in .env next to parser.py or export ...)")
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("interrupted; everything already written to the DB is saved")
