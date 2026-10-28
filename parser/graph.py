#!/usr/bin/env python3
"""The indexing pipeline as a LangGraph graph: fan out, then caption -> postprocess -> persist."""
import asyncio
import logging
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import START, StateGraph
from langgraph.types import Command, Send

import parser as P

log = logging.getLogger("pipeline.graph")


class St(TypedDict, total=False):
    item: "P.Item"


def build_pipeline(cfg, tg, session, emb, db, cache, raw_cache, packs, known,
                   counters, stop, joy_prompt, pp_prompt):
    """Each new sticker travels its own branch: scan fans out with Send, nodes
    update the Item in place and hand it further via Command(goto=...), so
    parallel branches never write to a shared channel.

    Returns (compiled graph, flush) - call flush() once the graph has finished."""
    sem_dl = asyncio.Semaphore(max(1, cfg.dl_concurrency))
    sem_joy = asyncio.Semaphore(max(1, cfg.joy_concurrency))
    sem_pp = asyncio.Semaphore(max(1, cfg.pp_concurrency))
    # an item holds a decoded image from download until the caption call: keep that window bounded
    flight = asyncio.Semaphore(max(4, cfg.dl_concurrency + cfg.joy_concurrency * 4))
    buf: list[P.Item] = []
    flush_lock = asyncio.Lock()
    pending: list[P.Item] = []

    async def flush() -> None:
        batch, buf[:] = buf[:], []
        if not batch:
            return
        try:
            await asyncio.to_thread(P.save_batch, db, emb, batch, counters,
                                    Path(cfg.model_dir).name)
        except Exception as e:
            counters["failed"] += len(batch)
            log.exception("batch write failed: %s", e)

    async def scan(_st: St):
        n = 0
        for idx, name in enumerate(packs):
            if stop.is_set():
                log.error("pack list interrupted by the quota stop: %d of %d packs left",
                          len(packs) - idx, len(packs))
                return Command()
            s = await tg.get_pack(name)
            if s is None:
                counters["packs_bad"] += 1
                log.warning("pack %s is unavailable", name)
                continue
            new = 0
            for st in s.stickers:
                if stop.is_set():
                    return Command()
                if getattr(st, "type", "regular") == "custom_emoji":
                    continue
                if st.file_unique_id in known:
                    counters["dup"] += 1
                    continue
                kind = "animated" if st.is_animated else ("video" if st.is_video else "static")
                src = st.file_id if kind == "static" else (st.thumbnail.file_id if st.thumbnail else None)
                if not src:
                    counters["no_thumb"] += 1
                    continue
                known.add(st.file_unique_id)      # skip the same sticker met again in this run
                pending.append(P.Item(st.file_unique_id, st.file_id, src, s.name, s.title,
                                      st.emoji or "", kind))
                new += 1
                n += 1
                if cfg.limit and n >= cfg.limit:
                    log.info("reached --limit %d", cfg.limit)
                    return Command(goto=[Send("caption", {"item": it}) for it in pending])
            log.info("pack %s: %d new of %d", name, new, len(s.stickers))
        if not pending:
            return Command()
        if stop.is_set():                     # quota hit while scanning: the tail is already lost
            counters["dropped"] += len(pending)
            return Command()
        return Command(goto=[Send("caption", {"item": it}) for it in pending])

    async def caption(st: St) -> object:
        it: P.Item = st["item"]
        if stop.is_set():
            counters["dropped"] += 1
            return Command()
        async with flight:
            try:
                if it.uid in raw_cache:       # caption already cached - the file is not needed
                    it.raw_caption = raw_cache[it.uid]
                    return Command(goto=[Send("postprocess", {"item": it})])
                async with sem_dl:
                    raw = await tg.download(it.src_file_id)
                b64 = await asyncio.to_thread(P.prepare_image, raw)
                async with sem_joy:
                    it.raw_caption = await P.joy_caption(session, cfg, joy_prompt, b64,
                                                         uid=it.uid)
                it.image_b64 = None           # the image is no longer needed
                cache.execute("INSERT OR REPLACE INTO raw VALUES (?,?,?)",
                              (it.uid, it.raw_caption, int(time.time())))
                cache.commit()
                counters["captioned"] += 1
            except P.QuotaExhausted as e:
                counters["failed"] += 1
                if not stop.is_set():
                    stop.set()
                    log.error("[caption] %s: %s - stopping the run: OpenRouter quota is "
                              "exhausted. Everything already written stays in the DB; "
                              "rerun later.", it.uid, e)
                return Command()
            except Exception as e:
                counters["failed"] += 1
                log.warning("[caption] %s: %s", it.uid, e)
                return Command()
        return Command(goto=[Send("postprocess", {"item": it})])

    async def postprocess(st: St) -> object:
        it: P.Item = st["item"]
        if stop.is_set():
            counters["dropped"] += 1
            return Command()
        try:
            async with sem_pp:
                rec = await P.postprocess(session, cfg, pp_prompt, it.raw_caption,
                                          uid=it.uid)
            rec["nsfw"] = P.classify(rec)
            it.rec = rec
        except P.QuotaExhausted as e:
            counters["failed"] += 1
            if not stop.is_set():
                stop.set()
                log.error("[postprocess] %s: %s - stopping the run: OpenRouter quota is "
                          "exhausted. Everything already written stays in the DB; "
                          "rerun later.", it.uid, e)
            return Command()
        except Exception as e:
            counters["failed"] += 1
            log.warning("[postprocess] %s: %s", it.uid, e)
            return Command()
        return Command(goto=[Send("persist", {"item": it})])

    async def persist(st: St) -> dict:
        it: P.Item = st["item"]
        if stop.is_set():
            counters["dropped"] += 1
            return {}
        async with flush_lock:
            buf.append(it)
            if len(buf) >= max(1, cfg.emb_batch):
                await flush()
        return {}

    g = StateGraph(St)
    g.add_node("scan", scan)
    g.add_node("caption", caption)
    g.add_node("postprocess", postprocess)
    g.add_node("persist", persist)
    g.add_edge(START, "scan")
    return g.compile(), flush
