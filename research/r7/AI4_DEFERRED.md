# AI4-DEEPLOB-1 deferred to v1.16 (owner decision, 2026-10-12)

`research/r7/ai4_deeplob_prereg.json` was signed on 2026-10-12 (board seq 9,
ledger key `3060ab23…`, one look spent). It is **not run in v1.15** and its
holdout test is **not performed** under this registration.

Why: the v1.15 review found the AI4 pipeline cannot honour the registration as
written:

1. No real-data path. `iap.models.deeplob.build_lob_dataset` has no caller; nothing
   turns ITCH book replay into the (T, 4L) snapshot matrix.
2. Windows and smoothed labels are built over one continuous series, so on
   multi-day data they cross the overnight gap and the auctions.
3. Price levels use one static train z-score (drifts out of sample); empty deep
   levels (0/NaN) are not handled.

Fixing (2) and (3) changes the model inputs the prereg pins, so the decision was
to defer, not to file a deviation.

v1.16 plan: build the real-data adapter with per-session dataset construction,
mid-relative or rolling normalisation and empty-level masking; then register a
**new** AI4 prereg that supersedes this one (the look spent here stays counted).
The 2025-11 → 2026 holdout sessions must not be used for any AI4 development
before that new registration.
