# Sealed data: identities, partitions, the service, and the single-machine limit

v2.0 stage 2, D2 (U0 section 2; v1.19 specification revision 9 D2 as carried). Code:
[`tools/sealed_service.py`](../../tools/sealed_service.py) (service, client framing, checkpoint rules),
[`tools/admin/create_sealed_accounts.ps1`](../../tools/admin/create_sealed_accounts.ps1) (accounts and ACLs,
run by the owner on 2026-10-25, decision record DR-5),
[`tools/admin/isolation_probe.py`](../../tools/admin/isolation_probe.py) and
[`python/tests/isolation/`](../../python/tests/isolation/) (the identity x partition matrix and the service
core tests). The runner that consumes the service is in [RUNNER_THREAT_MODEL.md](RUNNER_THREAT_MODEL.md).

## 1. What exists on the workstation

| identity | kind | owns | may do |
|---|---|---|---|
| the owner's normal Windows user (`iap-dev` by role) | development identity | the repository checkout, the research data under `data/` | everything in-sample; **nothing** under `C:\iap-sealed` |
| `iap-sealed-a` | standard local user | `C:\iap-sealed\a` (Track A, the H1 window) | runs one `sealed_service.py serve --partition a` |
| `iap-sealed-b` | standard local user | `C:\iap-sealed\b` (Track B, the ML test year) | runs one service for `b` |
| `iap-sealed-c` | standard local user | `C:\iap-sealed\c` (Track C, forward confirmation) | runs one service for `c` |
| `iap-runner` | standard local user | `C:\iap-sealed\runner` (its private cache) | the self-hosted GitHub runner; the only account the services answer |

Each partition's access list names its owner and SYSTEM only; `BUILTIN\Administrators` was removed as the last
step of the script and the elevated administrator is denied read (observed by the owner, DR-5). The development
identity is denied read, write, enumeration and the two indirect routes (a report path and a cache directory
built under a partition) on all four partitions; the committed test matrix is `test_identity_partitions.py`
(the development half runs on the owner's machine without credentials; the sealed and runner halves need the
accounts' passwords in a file named by `IAP_ISOLATION_CREDENTIALS` and are skipped otherwise).

## 2. The service

One instance per partition, started under that partition's identity, listening on the named pipe
`\\.\pipe\iap-sealed-<partition>`. The pipe is the only route from any other identity to sealed bytes.

**Authentication is the operating system's.** The service impersonates the connected client just long enough to
read the client token's account name and reads the client's process id from the pipe; it refuses any account
other than `iap-runner` and (when `registered_images` is configured) any client whose process image hash is not
a registered runner binary. There is no bearer token and no per-boot token file: a client cannot assert an
identity, the kernel reports it.

**Authorisation is service-side**, in this order, every step audited:

1. caller account, then process image;
2. no pending checkpoint (a read whose checkpoint the runner has not yet published blocks every further read;
   a restart keeps the pending state and re-announces it);
3. the request carries a **signed** prereg (Ed25519 under the trust-root copy pinned in the partition at install),
   whose `registered_run` block names this partition and the requested `dataset_hash_id`;
4. the registration ledger (keyed by the prereg's canonical sha256 and indexed by prereg id) has seen neither:
   **one run per registration**; a second request with the same hash, or the same prereg id re-signed at a new
   commit, is refused and logged;
5. every file of the named dataset still matches its recorded sha256 (**hash verification precedes any read**);
   a mismatch is recorded as a failed registration and cannot be retried without a new signed registration;
6. the ledger entry, the hash-chained audit line and the pending checkpoint are written **before the first byte
   is served** (write-before-serve).

The dataset is streamed file by file with a header (`name`, `size`, `sha256`) the client verifies on arrival.
After the run the runner stores the result hash (`store_result`) and publishes the checkpoint with the commit
that holds it (`publish`); `verify_result` compares a committed hash with the service's copy.

## 3. Checkpoints and the repository anchor

A checkpoint is exactly `{seq, chain_head, utc, prereg_id, dataset_hash_id}`: the audit chain head after the
served read, and two identifiers already public in the repository. It never carries a path, a credential, a
host name or an account name (`checkpoint_problems` enforces the rule; a test greps the committed file). The
runner appends each checkpoint to `research/audit/CHECKPOINTS.jsonl` on the `results` branch, so the sealed
service's private audit chain has a public anchor: `python tools/sealed_service.py verify-checkpoints` checks
the committed file (anyone, CI) and `verify --root <partition>` checks the local chain (the sealed identity).

## 4. The single-machine limit

Everything above runs on one workstation, and the owner is that workstation's administrator, developer,
approver and the holder of every sealed credential. The controls stop the **development identity's processes**:
the owner's own user cannot read, list or write a partition, cannot connect to the service, and cannot start a
registered run without the GitHub environment approval. They do not stop the administrator: an administrator
can take ownership of a partition, read the service's state, reset an account's password or stop the service.
Nothing in this repository claims otherwise. Consequences, stated plainly:

- the evidence class of anything produced through this machinery is **mechanically isolated**, not
  **independently confirmed** (U0 section 2 property 6; section 10 wording); the class upgrades only when an
  approval or custody authority other than the developer exists (owner item O-3);
- the audit chain is tamper-evident, not tamper-proof: a person with administrator rights can rewrite it from
  line one; the committed checkpoints make a rewrite after a publish detectable, not impossible (D-24 stands);
- encryption at rest of the partitions is an operating-system setting (BitLocker or EFS), not something the
  account script enables (defect D-52);
- the process-image check depends on the service being allowed to query the client process; when Windows
  denies that query between two standard users the service refuses (fail closed) and the owner either grants
  the query right or leaves `registered_images` empty and relies on the account check alone (D-48).

## 5. Install (owner)

1. Accounts and partitions: `tools/admin/create_sealed_accounts.ps1` (done, DR-5).
2. For each partition `x` in `a`, `b`, `c`, as `iap-sealed-x`: create `C:\iap-sealed\x\service\`, copy the
   repository's `research/agents/trust_roots.json` there, write `config.json` =
   `{"allowed_accounts": ["iap-runner"], "registered_images": []}` (add the sha256 of the runner's
   `python.exe` to enable the image check), record each ingested dataset with
   `python tools/sealed_service.py add-dataset --root C:\iap-sealed\x --hash-id <id> --dir <dataset dir>`.
3. Start the service as the sealed identity (a scheduled task "at log on" for that account, or a service
   wrapper): `python tools/sealed_service.py serve --partition x --root C:\iap-sealed\x`. The interpreter and
   the repository checkout must be readable by the sealed accounts (an installation outside the owner's profile).
4. `iap-runner` reaches the service only through the pipe; it has no entry in any partition ACL except its own.

Ingestion of the free-route daily set into the partitions (DD1) is a separate PR and runs as `iap-runner`.
