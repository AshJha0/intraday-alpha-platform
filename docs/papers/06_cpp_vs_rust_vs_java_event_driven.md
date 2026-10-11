

## Erratum / Update — 2026-10-11 (v1.14.0: the correctness release)

The body and earlier errata are left as published; the benchmark figures
are unchanged. v1.14 is a useful data point for the paper's argument about
keeping four implementations in step. Three pull requests changed shared
semantics (same-timestamp coalescing, the MODIFY queue credit, the risk
audit chain and SEC 15c3-5 checks) and had to port them to the frozen Java
copies under the `POLYGLOT-OVERRIDE` rule. None of them could build C++,
Rust or Java on the author's machine; CI and the regenerated goldens were
the only cross-language proof. That is the cost of the polyglot design
stated plainly: a semantic change is three or four changes, and the
goldens are what make that safe rather than merely laborious.
