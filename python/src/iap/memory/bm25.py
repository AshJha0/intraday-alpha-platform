"""Okapi BM25 over a fixed list of documents, written in-house.

score(D, Q) = sum over distinct terms q of Q of
    IDF(q) * f(q, D) * (k1 + 1) / (f(q, D) + k1 * (1 - b + b * |D| / avgdl))
IDF(q) = ln(1 + (N - n(q) + 0.5) / (n(q) + 0.5))

``f(q, D)`` is the count of ``q`` in ``D``, ``|D|`` the token count of ``D``,
``avgdl`` the mean of ``|D|``, ``N`` the number of documents and ``n(q)`` the
number containing ``q``.  Query terms are summed in sorted order and ties are
broken by document index, so a ranking is a pure function of its inputs.
"""

from __future__ import annotations

import math
import re
from collections import Counter

K1 = 1.5
B = 0.75

# An identifier is letters/digits joined by '-', '_' or '.' (EQ10, REL-08,
# 15m, v1.14, gate_looks).  It is lower-cased and kept whole; its hyphen and
# underscore parts are emitted as well so "net-of-cost" also matches "cost".
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
_SPLIT = re.compile(r"[-_]")

STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "this to was were what which with does did do how why when where who".split()
)


def tokenize(text: str) -> list[str]:
    """Lower-cased tokens of ``text``; identifiers stay whole."""
    out: list[str] = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0).lower()
        if tok in STOPWORDS:
            continue
        out.append(tok)
        if "-" in tok or "_" in tok:
            out.extend(p for p in _SPLIT.split(tok) if p and p not in STOPWORDS)
    return out


class BM25:
    """An inverted index over pre-tokenised documents."""

    def __init__(self, docs: list[list[str]], k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self.n_docs = len(docs)
        self.lengths = [len(d) for d in docs]
        self.avgdl = (sum(self.lengths) / self.n_docs) if self.n_docs else 0.0
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            for term, tf in sorted(Counter(d).items()):
                self.postings.setdefault(term, []).append((i, tf))

    def idf(self, term: str) -> float:
        df = len(self.postings.get(term, ()))
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def scores(self, query: list[str]) -> dict[int, float]:
        """Score of every document containing at least one query term."""
        acc: dict[int, float] = {}
        if not self.n_docs or self.avgdl == 0:
            return acc
        for term in sorted(set(query)):
            post = self.postings.get(term)
            if not post:
                continue
            idf = self.idf(term)
            for i, tf in post:
                norm = self.k1 * (1.0 - self.b + self.b * self.lengths[i] / self.avgdl)
                acc[i] = acc.get(i, 0.0) + idf * tf * (self.k1 + 1.0) / (tf + norm)
        return acc

    def top(
        self, query: list[str], k: int, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        """The ``k`` best (index, score), score descending, index ascending on ties."""
        s = self.scores(query)
        items = [(i, v) for i, v in s.items() if allowed is None or i in allowed]
        items.sort(key=lambda t: (-t[1], t[0]))
        return items[:k]
