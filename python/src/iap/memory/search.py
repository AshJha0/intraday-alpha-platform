"""Search, the prior-tests gate, and the retrieval eval."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from iap.governance.untrusted import wrap
from iap.memory.bm25 import BM25, tokenize
from iap.memory.corpus import Chunk, build_corpus

REPO_ROOT = Path(__file__).resolve().parents[4]
EVAL_QUESTIONS = "research/memory/eval_questions.json"
EVAL_RESULT = "research/memory/eval_result.json"
SNIPPET_CHARS = 320
KINDS = ("doc", "research", "ledger")


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk with its citation."""

    path: str
    line_start: int
    line_end: int
    heading: str
    score: float
    snippet: str
    kind: str
    alpha_id: str = ""

    @property
    def citation(self) -> str:
        if self.kind == "ledger":
            return self.path
        return f"{self.path}:{self.line_start}-{self.line_end}"

    def as_json(self) -> dict[str, Any]:
        """The hit with its free text wrapped as untrusted data."""
        d = asdict(self)
        source = f"{self.path}:{self.line_start}"
        d["heading"] = wrap(self.heading, f"{source}#heading")
        d["snippet"] = wrap(self.snippet, source)
        d["score"] = round(self.score, 6)
        return d


def _index_text(c: Chunk) -> list[str]:
    # The path and the heading carry the most information per token; the
    # heading is counted twice so a section named for the query outranks a
    # passing mention.
    return tokenize(c.path) + tokenize(c.heading) * 2 + tokenize(c.text)


def _snippet(text: str, terms: set[str]) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    best, best_n = 0, -1
    for i, ln in enumerate(lines):
        n = len(terms.intersection(tokenize(ln)))
        if n > best_n:
            best, best_n = i, n
    out = " ".join(ln.strip() for ln in lines[best : best + 4])
    return out if len(out) <= SNIPPET_CHARS else out[: SNIPPET_CHARS - 3] + "..."


class Memory:
    """The research record of one working tree, indexed in memory."""

    def __init__(self, root: Path = REPO_ROOT, chunks: list[Chunk] | None = None) -> None:
        self.root = Path(root)
        self.chunks = build_corpus(self.root) if chunks is None else list(chunks)
        self.index = BM25([_index_text(c) for c in self.chunks])
        self._by_kind = {k: {i for i, c in enumerate(self.chunks) if c.kind == k} for k in KINDS}

    def _allowed(self, kinds: list[str] | tuple[str, ...] | None) -> set[int] | None:
        if not kinds:
            return None
        bad = set(kinds) - set(KINDS)
        if bad:
            raise ValueError(f"unknown kinds {sorted(bad)}; expected some of {KINDS}")
        return set().union(*(self._by_kind[k] for k in kinds))

    def _hit(self, i: int, score: float, terms: set[str]) -> Hit:
        c = self.chunks[i]
        return Hit(
            c.path,
            c.line_start,
            c.line_end,
            c.heading,
            score,
            _snippet(c.text, terms),
            c.kind,
            c.alpha_id,
        )

    def search(
        self, query: str, k: int = 10, kinds: list[str] | tuple[str, ...] | None = None
    ) -> list[Hit]:
        """The ``k`` best chunks for ``query``, optionally limited to ``kinds``."""
        q = tokenize(query)
        terms = set(q)
        return [self._hit(i, s, terms) for i, s in self.index.top(q, k, self._allowed(kinds))]

    def prior_tests(
        self, alpha: str | None = None, horizon: str | None = None, text: str = "", k: int = 10
    ) -> dict[str, list[Hit]]:
        """Ledger entries and result documents nearest to a proposed test.

        With ``alpha`` the ledger side is restricted to entries of that
        alpha_id (and, with ``horizon``, to entries whose configured horizon
        starts with it); the documents side is ranked by the same words.
        """
        words = " ".join(x for x in (alpha, horizon, text) if x)
        if not words.strip():
            raise ValueError("prior-tests needs --alpha, --horizon or free text")
        q = tokenize(words)
        terms = set(q)
        allowed = set(self._by_kind["ledger"])
        if alpha:
            allowed = {i for i in allowed if self.chunks[i].alpha_id.lower() == alpha.lower()}
        if horizon:
            allowed = {
                i for i in allowed if self.chunks[i].horizon.lower().startswith(horizon.lower())
            }
        scored = dict(self.index.scores(q))
        led = sorted(allowed, key=lambda i: (-scored.get(i, 0.0), i))[:k]
        docs = self.index.top(q, k, self._by_kind["research"] | self._by_kind["doc"])
        return {
            "ledger": [self._hit(i, scored.get(i, 0.0), terms) for i in led],
            "documents": [self._hit(i, s, terms) for i, s in docs],
        }


_DEFAULT: Memory | None = None


def default_memory() -> Memory:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Memory(REPO_ROOT)
    return _DEFAULT


def search(query: str, k: int = 10, kinds: list[str] | tuple[str, ...] | None = None) -> list[Hit]:
    """Search the repository's research record (built once per process)."""
    return default_memory().search(query, k=k, kinds=kinds)


# --------------------------------------------------------------------- eval


def _matches(hit: Hit, accept: dict[str, Any]) -> bool:
    want = accept["path"]
    path = hit.path if "#" in want else hit.path.split("#", 1)[0]
    if path != want:
        return False
    if "heading" in accept and accept["heading"].lower() not in hit.heading.lower():
        return False
    return not ("alpha_id" in accept and accept["alpha_id"] != hit.alpha_id)


def run_eval(memory: Memory, spec: dict[str, Any], k: int = 10) -> dict[str, Any]:
    """Recall@1/5/10 and MRR of ``memory`` on the questions in ``spec``."""
    rows = []
    for q in spec["questions"]:
        hits = memory.search(q["question"], k=k, kinds=q.get("kinds"))
        rank = next(
            (r for r, h in enumerate(hits, 1) if any(_matches(h, a) for a in q["accept"])), None
        )
        rows.append(
            {
                "id": q["id"],
                "category": q["category"],
                "rank": rank,
                "top1": hits[0].citation if hits else None,
            }
        )
    n = len(rows)

    def recall(cut: int) -> float:
        return round(sum(1 for r in rows if r["rank"] and r["rank"] <= cut) / n, 4)

    return {
        "n_questions": n,
        "recall_at_1": recall(1),
        "recall_at_5": recall(5),
        "recall_at_10": recall(10),
        "mrr": round(sum(1.0 / r["rank"] for r in rows if r["rank"]) / n, 4),
        "recall_at_5_floor": spec["recall_at_5_floor"],
        "n_chunks": len(memory.chunks),
        "missed_at_5": [r["id"] for r in rows if not r["rank"] or r["rank"] > 5],
        "questions": rows,
    }


def load_spec(root: Path) -> dict[str, Any]:
    return json.loads((root / EVAL_QUESTIONS).read_text(encoding="utf-8"))
