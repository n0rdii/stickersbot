"""Typo correction for the FTS channel (Damerau-Levenshtein over the index vocabulary)."""
from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from typing import Callable

MIN_LEN = 5               # at distance 1 a 4-letter word ("helo") has too many wrong neighbours
MIN_COUNT = 2            # a word seen once is as likely OCR noise as a real word
_LATIN = re.compile(r"[a-z]+")


def osa(a: str, b: str, maxd: int) -> int:
    if abs(len(a) - len(b)) > maxd:
        return maxd + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        lo = i
        for j in range(1, len(b) + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)          # swapped neighbours: "cheetha"
            cur[j] = v
            lo = min(lo, v)
        if lo > maxd:
            return maxd + 1
        prev2, prev = prev, cur
    return prev[-1]


class SpellFixer:
    def __init__(self, words: Counter, is_known: Callable[[str, bool], bool]):
        self.words = {w: c for w, c in words.items() if _LATIN.fullmatch(w)}
        self._sorted = sorted(self.words)
        self._by_len: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for w, c in self.words.items():
            if c >= MIN_COUNT:
                self._by_len[len(w)].append((w, c))
        self.is_known = is_known
        self._cache: dict[tuple[str, bool], str] = {}

    def _prefix_in_vocab(self, tok: str) -> bool:
        i = bisect.bisect_left(self._sorted, tok)
        return i < len(self._sorted) and self._sorted[i].startswith(tok)

    def correct_token(self, tok: str, being_typed: bool = False) -> str:
        if len(tok) < MIN_LEN or not _LATIN.fullmatch(tok):
            return tok
        key = (tok, being_typed)
        if key in self._cache:
            return self._cache[key]
        out = tok
        if not (tok in self.words or self.is_known(tok, False)
                or (being_typed and (self._prefix_in_vocab(tok) or self.is_known(tok, True)))):
            maxd = 1 if len(tok) < 8 else 2
            best: tuple[int, int, str] | None = None
            for ln in range(len(tok) - maxd, len(tok) + maxd + 1):
                for cand, cnt in self._by_len.get(ln, ()):
                    d = osa(tok, cand, maxd)
                    if d <= maxd and (best is None or (d, -cnt) < best[:2]):
                        best = (d, -cnt, cand)
            if best:
                out = best[2]
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = out
        return out

    def correct_query(self, text: str) -> str:
        toks = re.findall(r"\w+", text.lower())
        fixed = [self.correct_token(t, being_typed=(i == len(toks) - 1))
                 for i, t in enumerate(toks)]
        return " ".join(fixed) if fixed != toks else text

