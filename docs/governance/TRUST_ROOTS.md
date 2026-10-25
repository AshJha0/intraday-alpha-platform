# Trust roots and model promotion (v1.15, G-P1-2 / G-P0-1)

Pinned version, key fingerprint, rotation and revocation: [TRUST_ROOT.md](TRUST_ROOT.md).
Verify everything (full-history clone): `PYTHONPATH=python/src python tools/verify_signatures.py --trust-ref origin/main --strict`.

## What a signature proves, and the four controls it is not (v2.0 stage 11, G1)

An Ed25519 signature under a maintainer key proves **integrity**: the signed document (a
pre-registration, a rule, the key registry) did not change after it was signed, and the signer held
the key. It proves nothing about who ran what, or whether anyone outside the project looked. The
v1.19 review (plan section 2.3) found the words "signer != runner" read as an independence claim
where the mechanism was an environment variable; that wording is retired. The programme keeps four
controls apart, each with its own mechanism and its own evidence class (U0 section 10):

| control | what it establishes | mechanism | status on `main` (2026-10-25) |
|---|---|---|---|
| **integrity** | the document is the one that was signed | this page: trust roots read from `origin/main`, `verify_signatures.py --strict`, the governance CI job | enforced |
| **execution separation** | the developer's identity could not read the sealed inputs or alter the run | operating-system accounts (`iap-dev`, `iap-sealed-{a,b,c}`, `iap-runner`), the sealed service over a named pipe, the registered runner CR1 ([RUNNER_THREAT_MODEL.md](RUNNER_THREAT_MODEL.md)); **not** an environment variable | built and tested on synthetic data; no registered run executed; repository settings of REPO_SETTINGS.md section 7 not yet set |
| **independent review** | a reviewer approved the protocol before the run | the outside reviewer's written decisions (`docs/release/REVIEWER_*`), the `sealed` environment's required reviewer | decisions on file for F3 and REL-08; the environment does not exist yet |
| **independent reproduction** | a party outside the project recomputed the result | P2 ([../REPRODUCTION.md](../REPRODUCTION.md) section 6); gate F7 | open: the pinned image reproduces B8 v2 and the REL-08 scorecard "in the pinned image by CI", no independent party has run it |

A result's class follows from which controls held when it was produced, never from the signature
alone: *independently confirmed* needs execution separation under custody separate from the
developer plus independent review; *mechanically isolated* is CR1 under the developer's custody;
everything produced so far is at most *development-supported*. `iap.agents.analysis_identity.require_independent_runner`
remains in the code as the tamper-evidence check it always was (the declared actor and the absence
of a signing key in the runner's environment); it is not cited as an independence control anywhere
in the documentation.

## What is enforced

- `research/agents/trust_roots.json` lists the maintainers' Ed25519 **public**
  keys. Since v1.15 it is non-empty (maintainer `ashjha0`).
- `research/agents/agent_pubkeys.sig.json` is a maintainer signature over the
  canonical digest of `research/agents/agent_pubkeys.json`. It is required
  even while the registry is empty.
- CI (`governance` job) runs
  `python tests/harness/check_agent_keys.py --trust-ref origin/main --require-signed`
  and `python -m iap.agents.cli verify-board --trust-ref origin/main`. Trust
  roots are read from `origin/main`, so a pull request cannot vouch for a key
  it adds. An unsigned or stale-signed registry, or no trust root at all,
  fails the job.
- Bootstrap: only while `origin/main` lists no maintainer, the working tree's
  roots are used (a warning is printed). After the PR introducing the first
  key merges, this never fires again.
- Model promotion (`python -m iap.mlops promote <id>`,
  `ModelRegistry.promote`) refuses a model whose `content.json` is missing or
  does not match the hash re-derived from the verified artefact, and writes
  `promotion.json`. CI runs `python -m iap.mlops check-promotions`, which
  fails if any promoted model no longer matches.
- Promotion also requires a valid model card (`model_card.json`,
  `iap.mlops.model_card`; write it with `python -m iap.mlops card <id> --spec
  <json>`): data/sessions, prereg id and hash, metrics net of costs,
  calibration (or null), known limitations, intended use and the owner
  sign-off reference, bound to the record hash. A missing or invalid card is
  a `RegistryError`; `check-promotions` re-validates it.

## Where the private key lives

`C:/Users/ashis/.iap_keys/maintainer.key` (JSON `{maintainer id: private key
hex}`), outside the repository. Never commit it, paste it or print it. Keep
an offline backup (encrypted USB / password manager secure note).

## Re-signing after a registry change

```
PYTHONPATH=python/src python -m iap.agents.cli sign-registry \
  --maintainer ashjha0 --keyfile C:/Users/ashis/.iap_keys/maintainer.key
```

Commit the updated `agent_pubkeys.sig.json` with the registry change.

## Rotation (planned)

1. Generate the new key under a new id:
   `python -m iap.agents.cli maintainer-keygen --maintainer ashjha0-2027 --keyfile <new keyfile outside the repo>`
   (adds the public key to `trust_roots.json`).
2. Open a PR with only the `trust_roots.json` change; the old key keeps the
   registry trusted while it is reviewed. Merge it.
3. Re-sign the registry with the new key (`sign-registry --maintainer ashjha0-2027 ...`)
   and, in the same PR, remove the old id from `trust_roots.json`. CI reads
   the roots from `origin/main`, which still lists both, so the new signature
   verifies. Merge.
4. Retire the old private key file (keep the backup until the rotation is on main).

## Restore / compromise

- **Lost key, backup available:** copy the backup to the keyfile path; nothing
  in the repository changes.
- **Lost key, no backup:** the registry cannot be re-signed. The repository
  owner (who controls branch protection) edits `trust_roots.json` to a newly
  generated key and re-signs in one PR, merged with an admin review; until it
  lands on main CI keeps trusting the old root, so the PR's governance check
  fails by design and needs the owner's override. Record the event in the
  CHANGELOG.
- **Compromised key:** same as "no backup", and additionally review every
  registry change signed since the suspected compromise.
