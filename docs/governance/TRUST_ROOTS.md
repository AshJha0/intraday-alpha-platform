# Trust roots and model promotion (v1.15, G-P1-2 / G-P0-1)

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
