# Owner signature checklist: what you check before signing a v2.0 prereg

## The idea

Your signature on a prereg says: "this exact analysis, on this exact sealed dataset, with these exact decision rules, may
be run once, and its answer stands whichever way it comes out." Nothing after the signature can change the question.
So everything worth checking has to be checked before you sign. The tooling checks the hashes; you check the meaning.
This page lists both, in the order you meet them, per confirmation profile.

Signing is yours alone (U0 DR-2: signatures with the owner's key are reserved to the owner). No session signs on your
behalf, and the templates in `research/v2/prereg_templates/` are unsigned and stay so.

## Formal statement

A prereg is ready for your signature when every item below holds. Items marked **(tool)** are refused mechanically by
`tools/prereg_from_template.py` or `tools/prereg_bind_identity.py sign` if they fail; you confirm the tool ran clean.
Items marked **(you)** are judgement no tool makes.

### A. Common to every profile

| # | check | who |
|---|---|---|
| A1 | The prereg was filled from the template of the profile it names (`template.path`, `template.sha256`), and that template is the committed one. | (tool) |
| A2 | The family (`registration.family`) is the set of hypotheses you meant to test, named before the window opens, none added because of something seen. | (you) |
| A3 | The protocol document (`protocol`) says in words what each hypothesis claims and how a result would be read; its hash is bound (`protocol_sha256`). | (you) reads; (tool) hashes |
| A4 | The economic screen is the one you would defend in public: `capital_usd` is the capital you would actually commit; `hurdle_bps` and `margin` were fixed without looking at the confirmation data. | (you) |
| A5 | The partition is right: `b` for a Track B test (year Y-2), `c` for a Track C confirmation (Y-1 onward). A Track C registration never names `b`. | (you); (tool) refuses `a` |
| A6 | `data.origin` is `real` and `dataset_hash_id` is the id the data manifest records for that partition. `synthetic` is for dry registrations only and must never be signed. | (you) |
| A7 | The planted dry run is bound (`dry_run`) and shows every verdict value the rule can return. | (tool): sign refuses otherwise |
| A8 | The analysis identity verifies at HEAD: scripts, code tree, configs, reference data, lock, installed packages, interpreter, image digest. | (tool): sign re-verifies |
| A9 | The working tree is clean and the prereg is not already signed. | (tool) |
| A10 | The key used is yours (`research/agents/trust_roots.json`), on your machine. | (you) |
| A11 | You accept that the evidence class of the run's result is **mechanically isolated**, not independently confirmed, while you hold development, approval and sealed custody alone (U0 section 2 item 6). | (you) |

### B. PF-1: a 1-5 session book

| # | check | who |
|---|---|---|
| B1 | `horizon_sessions` is the book's real holding horizon, between 1 and 5. | (you) checks meaning; (tool) refuses outside 1-5 |
| B2 | `procedure` is `P-HAC-L5` with `hac_lag` 5 (fixed by the template). | (tool) |
| B3 | `T_min` is at least 120, and you are content to wait for that many usable sessions; the window may run to 1.25 x `T_min` attempts, after which the result is `confirmation pending` with nothing examined. | (you); (tool) refuses below 120 |

### C. PF-2: a 20-60 session book with overlapping positions

| # | check | who |
|---|---|---|
| C1 | `horizon_sessions` is between 20 and 60 and matches how long positions are actually held. | (you); (tool) range |
| C2 | `procedure` is `P-HAC-L20` with `hac_lag` 20 (fixed by the template). `P-HAC-L60` and the bootstrap have no validated operating domain and are not available. | (tool) |
| C3 | `T_min` is at least 250 sessions, roughly a year of daily data. Check the partition holds that many before signing: a registration that cannot reach `T_min` ends `pending`, and the read is still spent. | (you) |
| C4 | Overlapping positions are the reason for this profile; a book with 1-5 session holdings belongs under PF-1 instead. | (you) |

### D. PF-3: the combined book

Do not sign. PF-3 has no validated profile; the template is a placeholder, filling it refuses, and the profile gate
refuses every PF-3 registration. Track C forward accrual does not count until PF-3 is designed, studied and frozen by
the reviewer.

### E. After signing

1. Commit the signed prereg and open its PR; merge it when CI is green.
2. Approve the `sealed` environment deployment of `registered-run.yml` for that prereg only.
3. The runner reads once. A second request with the same prereg hash is refused by the sealed service. If anything
   fails after the read, the registration is spent; a re-run needs a new signed prereg, and the failure is recorded.

## Worked example

You are asked to sign `research/preregs/d1_track_b.json`, filled from `PF-1.template.json`. You open it and see
`registration.family: ["D1-a", "D1-b"]`, `horizon_sessions: 3`, `T_min: 120`, `capital_usd: 1000000`,
`hurdle_bps: 1.0`, `margin: 0.25`, `data: {origin: "real", partition: "b", dataset: "dd1-daily"}`, a `dry_run` block
with verdict values `PROMOTE`, `REJECT`, `refused`, and `status: "DRAFT: ... NOT signed"`.

- A2: the family names two variants. If you recall a third variant was dropped after a development look, stop: that
  choice belongs in the protocol with its reason, and the multiplicity is the reader's to judge.
- A5: Track B, partition `b`. Correct for a Track B test.
- B3: 120 sessions of Y-2. A daily partition of one year has about 250 sessions, so the minimum is reachable.
- You run `python tools/prereg_bind_identity.py sign --prereg research/preregs/d1_track_b.json`. It prints the dry run
  and verdict values and writes the signature. Had the protocol changed since filling, it would have printed
  `REFUSED: research/v2/families/... changed since the identity was bound` and written nothing.
