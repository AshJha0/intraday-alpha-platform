# Trust root: maintainer key, pinned version, rotation and revocation

Outside reviewer, 2026-10-18 (trust point): an independently authenticated maintainer-key
fingerprint, a pinned trust-root version, and a key rotation and revocation procedure. The key
registry mechanics (agent keys, blackboard signatures, where the private key lives) are in
[TRUST_ROOTS.md](TRUST_ROOTS.md).

## The pinned trust root (version 1)

| Field | Value |
|---|---|
| Trust-root version | **1** (in force since commit `1d48e17`, 2026-10-11) |
| File | `research/agents/trust_roots.json` |
| Canonical sha256 of the file (`json.dumps(sort_keys=True, separators=(",", ":"))`) | `6fb5818c1303c81f74118e64302bb44916e618b755f1af3907720504a11283bb` |
| Maintainer id | `ashjha0` |
| Algorithm | Ed25519 |
| Public key (hex, 32 bytes) | `02a3e278cda3efac7dcd266fb8a3821a1415f0725a73f8b255aac84083c2106c` |
| **Fingerprint** (sha256 of the 32 raw public-key bytes) | `0723075249ff61946e0416f56c681cc961fba94beb13913a2c1c0f398b41eb3f` |
| Revoked keys | none |

The same values are pinned in `PINNED` in [tools/verify_signatures.py](../../tools/verify_signatures.py).
The tool fails when the trust root differs from the pin, lists a maintainer that is not pinned, or
still lists a revoked key. A test checks that the pin, this table and the file agree.

Recompute the fingerprint yourself:

```bash
python -c "import hashlib,json;k=json.load(open('research/agents/trust_roots.json'))['maintainers']['ashjha0'];print(hashlib.sha256(bytes.fromhex(k)).hexdigest())"
```

## Verify every signature

```bash
PYTHONPATH=python/src python tools/verify_signatures.py --trust-ref origin/main --strict
```

This checks the trust root against the pin, every tracked signed JSON document (preregs, the REL-08
rule), the agent key registry signature, the main blackboard (chain, anchors, agent signatures) and the
archived fork boards. It prints `OK` or `FAIL <reason>` per item and exits 1 if anything fails. A blackboard anchor whose commit is not in the clone (a shallow fetch) is reported as
`UNVERIFIABLE-HERE`, never `OK`; `--strict` (reviewer and release, on a full-history clone) makes that
fatal too. Signature, chain and trust-root problems always fail.

## Why the repository alone is not an independent anchor

Everything above lives in the repository. Anyone who can rewrite the repository can replace the key,
the pin and this page together, and every check would still pass. The anchor is independent only if
the fingerprint is also published somewhere the repository cannot change, under the owner's own
account, so that a reviewer can compare the two.

### What the owner must publish (owner action, not done yet)

Publish the block below **verbatim** in at least one place outside this repository that only the
owner controls. Two are better:

1. the GitHub profile README of `AshJha0` (the `AshJha0/AshJha0` repository), and
2. the notes of the GitHub release `v1.18.0` (or a pinned gist under the same account).

```text
intraday-alpha-platform maintainer signing key
trust-root version: 1
maintainer id:      ashjha0
algorithm:          Ed25519
public key:         02a3e278cda3efac7dcd266fb8a3821a1415f0725a73f8b255aac84083c2106c
fingerprint sha256: 0723075249ff61946e0416f56c681cc961fba94beb13913a2c1c0f398b41eb3f
trust_roots.json canonical sha256: 6fb5818c1303c81f74118e64302bb44916e618b755f1af3907720504a11283bb
revoked: none
```

Once it is published, add its URL(s) to the table below in a reviewed PR. A reviewer then compares
that out-of-repo block with this table and with `PINNED`. If they disagree, the reviewer should trust
none of the signatures.

| Published at | Date | By |
|---|---|---|
| *(pending: owner)* | | |

## Rotation (planned key change)

1. On the signing machine, generate the new key outside the repository
   (`python -m iap.agents.cli maintainer-keygen --maintainer ID --keyfile PATH`, PATH outside the repo; keep the private key next to the old one and back it up).
2. **Publish first.** Add the new fingerprint, as trust-root version N+1, to the out-of-repo
   locations above, next to the old one. Mark it "pending rotation" until step 5.
3. In one PR: add the new public key to `research/agents/trust_roots.json`. Bump `PINNED` (version,
   canonical sha256, maintainers) and the table on this page. Record the change in `CHANGELOG.md`.
   Sign the PR description, or a `research/agents/rotation_<N+1>.json` note, with the **old** key, so
   the old key vouches for the new one.
4. Re-sign the agent key registry (`agent_pubkeys.sig.json`) with the new key. Do **not** re-sign
   existing preregs. They stay valid history under the key that signed them, and the old key stays in
   the trust root as long as signed documents need it.
5. After merge, run `tools/verify_signatures.py --trust-ref origin/main`, then update the out-of-repo
   block to "version N+1 current".

## Revocation (key compromised or lost)

1. **Publish first.** Update the out-of-repo block: state the compromised fingerprint as REVOKED,
   with the date and the last commit you trust (the last one before any possible misuse).
2. In one PR: remove the key from `research/agents/trust_roots.json` (if it is retained only for
   history, it must be listed under `revoked`). Add its fingerprint to `PINNED["revoked"]`. Bump the
   version. Add a replacement key by the rotation steps above.
3. `tools/verify_signatures.py` then fails every document signed by the revoked key. For each one,
   the owner decides: re-sign with the new key after re-reviewing it (record the decision in
   `CHANGELOG.md`), or withdraw it. A confirmatory look that a revoked key authorised is treated as
   spent and unauthorised until it is re-reviewed.
4. Any commit after the last trusted commit that touches `research/agents/`, `research/preregs/`,
   `research/holdout_registry.json` or `tools/verify_signatures.py` must be audited before it is
   relied on.

## What code cannot enforce

The checks prove that signatures match the pinned key. They cannot prove who holds the private key,
or that the out-of-repo publication happened. Until the owner publishes the block, the trust root is
self-asserted.
