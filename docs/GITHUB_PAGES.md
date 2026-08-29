# Publishing this documentation with GitHub Pages

The `docs/` folder is Pages-ready: `docs/index.html` is a self-contained static
landing page (no build step, no Jekyll required) in the same style as the
Quant-Finance-Library site, and every link on it points at rendered Markdown on
GitHub, so nothing else needs generating.

## One-time setup

1. Push the repository to GitHub (e.g. `AshJha0/intraday-alpha-platform`).
   If you choose a different repository name, update the hard-coded links in
   `docs/index.html` (search for `AshJha0/intraday-alpha-platform`) and in
   this file.
2. On GitHub: **Settings → Pages → Build and deployment**:
   - Source: *Deploy from a branch*
   - Branch: `main`, folder: `/docs`
3. Save. The site appears at
   `https://<username>.github.io/intraday-alpha-platform/` within a minute or two.

## What gets served

| URL | content |
|---|---|
| `/` | `docs/index.html` — the landing page (numbers block, subsystem cards, quick start) |
| everything else | linked back to rendered Markdown on github.com (`LEARN.md`, `COOKBOOK.md`, `docs/ARCHITECTURE.md`, `docs/DIAGRAMS.md`, `docs/papers/…`, `docs/SPECIFICATION.md`) |

Mermaid diagrams in `docs/DIAGRAMS.md` and `docs/ARCHITECTURE.md` render natively
on github.com — no plugin needed. If you later want them rendered on the Pages
site itself, convert the page to Markdown with Jekyll's default theme or inline
`mermaid.js`; the current setup deliberately keeps `docs/index.html` dependency-free.

## Keeping the landing page honest

`docs/index.html` quotes real measured numbers (parity table, benchmark figures,
alpha verdicts). If you regenerate data, refit alphas, or re-run benchmarks,
re-check the numbers block against:

- `tests/harness/run_all.sh` output (test counts / parity),
- `research/alpha_reports/REPORT.md` (verdict counts),
- `benchmarks/RESULTS.md` (hot-path figures),
- `data/normalized/qc_report.json` and `data/reference/feature_registry.json`.

A wrong number on the landing page is a documentation bug — treat it like one.
