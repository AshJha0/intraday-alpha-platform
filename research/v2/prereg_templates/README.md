# Prereg templates per confirmation profile (v2.0 round 5)

## The idea

A registration under the confirmation framework has two kinds of field. Some are fixed by the profile it names: the
statistic, the test procedure, the HAC lag, the decision level, the sample-size cap, the refusal diagnostics. The rest
are choices the registration makes: which hypotheses, what horizon, how many sessions, what capital and hurdle, which
sealed dataset. A template writes the first kind down once, per profile, and leaves a named slot for each of the second
kind. Filling a template cannot change a fixed field, so a registration cannot quietly drift from the profile it was
validated under.

## Formal statement

| file | profile | procedure fixed | slots |
|---|---|---|---|
| [`PF-1.template.json`](PF-1.template.json) | PF-1, 1-5 session book | `P-HAC-L5`, `hac_lag` 5 | 16, listed by `slot_names` |
| [`PF-2.template.json`](PF-2.template.json) | PF-2, 20-60 session book, overlapping positions | `P-HAC-L20`, `hac_lag` 20 | the same 16 |
| [`PF-3.placeholder.json`](PF-3.placeholder.json) | PF-3, combined book | none: deferred | none; filling refuses |

Each procedure fixed above is the only candidate of its profile whose reference cells pass both the size and the power
acceptance in the committed evidence (`research/v2/confirmation_evidence/PF-1_RESULTS.json`, `PF-2_RESULTS.json`,
read through `iap.validation.profiles.operating_domain`; evidence class of that statement: development-supported,
synthetic Monte Carlo). The bootstrap candidates and `P-HAC-L60` have no operating domain and are not offered.

[`tools/prereg_from_template.py`](../../../tools/prereg_from_template.py) fills a template:

1. every slot takes exactly one value from the fill file; a missing or unknown key refuses;
2. the filled `registration` goes through the same checks `iap.validation.profiles.decide` applies before any
   statistic, against the committed evidence, so a fill the gate would refuse is refused at fill time;
3. the identity-v2 `analysis_identity` block is built over the bound scripts and inputs at HEAD, with `data_versions`
   equal to the dataset id and `image_digest` equal to the registered run's;
4. the prereg is written with `signed: false`, its `protocol` and `protocol_sha256`, and the planted dry-run block when
   `--planted-dry-run` is given (signing refuses without it).

The templates are unsigned and stay so. Signing a filled prereg is the owner's act, with
`tools/prereg_bind_identity.py sign`; see [`OWNER_SIGNATURE_CHECKLIST.md`](../../../docs/governance/OWNER_SIGNATURE_CHECKLIST.md).

## Worked example

A PF-1 registration of two 3-session reversal hypotheses on Track B, 120 sessions minimum, USD 1 m capital, 1 bp
hurdle with a 25 % margin. The fill file:

```json
{"horizon_sessions": 3, "family": ["D1-a", "D1-b"], "T_min": 120,
 "capital_usd": 1000000, "hurdle_bps": 1.0, "margin": 0.25, "min_units_per_session": 1,
 "origin": "real", "partition": "b", "dataset": "dd1-daily", "dataset_hash_id": "<from the data manifest>",
 "dataset_files": null, "image_digest": "<deployment/docker/IMAGE_DIGEST>",
 "command": ["python", "research/v2/families/run_d1.py"], "scripts": ["research/v2/families/run_d1.py"], "inputs": []}
```

`T_min: 119` would refuse (`sample-size rule missing or below the validated T`), and so would `horizon_sessions: 20`
(`horizon outside the profile`) or `partition: "a"`. A dry registration of exactly this path, on synthetic data and
signed with a test key, runs once through the runner and is refused the second time in
`python/tests/runner/test_template_registration.py` (evidence class of that statement: development-supported, a test
on fixtures).
