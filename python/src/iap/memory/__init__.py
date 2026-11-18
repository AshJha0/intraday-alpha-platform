"""Research memory: deterministic lexical retrieval over the research record.

Searches the repository's own Markdown (docs/, research/, top-level files)
and the experiment ledger with an in-house Okapi BM25, and cites every hit
by file and line.  No network, no model call, no third-party dependency.
See docs/RESEARCH_MEMORY.md.
"""

from iap.memory.bm25 import BM25, tokenize
from iap.memory.corpus import Chunk, build_corpus
from iap.memory.search import Hit, Memory, run_eval, search

__all__ = ["BM25", "Chunk", "Hit", "Memory", "build_corpus", "run_eval", "search", "tokenize"]
