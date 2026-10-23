"""Index-time text cleanup shared by the FTS index and the embeddings."""
from __future__ import annotations

import re

_IDLE = re.compile(
    r"\s*\b(?:(?:in|with|and)\s+)?(?:(?:an|a|the)\s+)?idle\s+(?:pose|stance|position)\b",
    re.IGNORECASE)
_POSTURE_IDLE = re.compile(r"\b(standing|sitting|lying|stands|sits)\s+idle\b", re.IGNORECASE)
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?])")
_PUNCT_RUN = re.compile(r"([,;])\s*(?=[.,;])")
_LEAD_JUNK = re.compile(r"^(?:with|and|in)\s+", re.IGNORECASE)


def strip_idle(text: str) -> str:
    if not text or "idle" not in text.lower():
        return text or ""
    out = _POSTURE_IDLE.sub(r"\1", _IDLE.sub("", text))
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _PUNCT_RUN.sub("", out)
    return " ".join(out.split())


def index_description(text: str) -> str:
    return strip_idle(text)


def index_list(items) -> list[str]:
    out: list[str] = []
    for x in items or []:
        if not isinstance(x, str) or x.strip().lower() in {"idle", "idling"}:
            continue
        y = _LEAD_JUNK.sub("", strip_idle(x)).strip(" ,;.")
        if y and y.lower() not in {"pose", "stance", "position"}:
            out.append(y)
    return out


# ------------------------------------------------------------------ FTS layout
FTS_LIST_KEYS = ("characters", "objects", "actions", "emotions", "visual_tags", "style_tags")
FTS_COLUMNS = ("description", *FTS_LIST_KEYS, "emoji", "set_title")
FTS_TOKENIZE = "porter unicode61 remove_diacritics 2"


def fts_create_sql(name: str = "fts_stickers") -> str:
    return (f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5("
            f"{', '.join(FTS_COLUMNS)}, tokenize='{FTS_TOKENIZE}')")


def fts_values(description: str, lists: dict, emoji: str, set_title: str) -> tuple:
    return (index_description(description),
            *[" ".join(index_list(lists.get(k))) for k in FTS_LIST_KEYS],
            emoji or "", set_title or "")
