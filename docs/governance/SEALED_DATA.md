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
2. An interpreter and a checkout the service accounts can read: CPython 3.12.10 for all users at `C:\Python312`
   with the pinned stack, and the repository cloned at `C:\iap\repo` (read-only to everyone but the owner):
   [`tools/admin/install_all_users_python.md`](../../tools/admin/install_all_users_python.md). The service resolves
   `python/src` from its own checkout (or `IAP_REPO` when run from a copy), so it runs from any location.
3. The services: [`tools/admin/install_sealed_services.ps1`](../../tools/admin/install_sealed_services.ps1)
   (elevated; asks for each sealed account's password). Per partition it runs `sealed_service.py install` **as the
   sealed identity** (a transient scheduled task: the administrator has no access to the partition by design), which
   creates `C:\iap-sealed\x\service\`, copies `research/agents/trust_roots.json` and writes `config.json` =
   `{"allowed_accounts": ["iap-runner"], "registered_images": []}` (add the sha256 of the runner's `python.exe` to
   enable the image check); runs `sealed_service.py check` the same way; registers the task "IAP sealed service x"
   = `python tools/sealed_service.py serve --partition x --root C:\iap-sealed\x` as that account, "run whether user
   is logged on or not", at system start-up (a service account never logs on, D-64), no time limit; starts it; and
   pings the pipe (`sealed_service.py ping`: the owner's own call gets the audited `caller_account` refusal, which is
   the proof the service is up). It also creates the development-readable `C:\iap\daily` (iap-runner writes, Users
   read) for the DD1 development and validation partitions.
4. Datasets enter a partition in one of two ways, both recorded in `service/datasets.json` with every file's sha256:
   the `ingest` endpoint (DD1: `iap-runner` streams the files, the service writes, re-hashes, checks the content hash
   and keeps the job's row counts; nothing is returned but hashes and those counts), or
   `python tools/sealed_service.py add-dataset --root C:\iap-sealed\x --hash-id <id> --dir <dataset dir>` run by the
   sealed identity for a dataset placed by other means.
5. `iap-runner` reaches the service only through the pipe; it has no entry in any partition ACL except its own.

The DD1 ingestion itself (the free daily route into the four partitions) is the owner's run of the `registered-run`
workflow in mode `dd1`: [DD1_INGESTION_RUNBOOK.md](DD1_INGESTION_RUNBOOK.md).

## 6. Backup and restore (owner)

The partitions have one copy each, on the laptop's own disk. A disk failure would lose Track B and Track C and the
audit chains that make every registered run checkable. The backup is the owner's job, run by hand; nothing in CI or
the runner does it.

**What is backed up.** All four partitions, `C:\iap-sealed\a`, `b`, `c` and `runner`: the datasets, each service's
state (`service\audit.jsonl`, `ledger.json`, `datasets.json`, any pending checkpoint, `config.json`, the pinned trust
roots) and the runner's cache. The repository, the committed checkpoints and the accounts' passwords are not in the
set: the repository is on GitHub, and the passwords belong in the owner's password manager.

**How often.** After each DD1 ingestion has finished and after each registered run has published its checkpoint.
Those are the only events that change a partition, so a backup taken then is complete until the next one. Never
during an ingestion or a run: the script stops the services for the copy.

**Where.** An external drive formatted NTFS (exFAT and FAT32 cannot hold the access lists), encrypted with BitLocker
To Go, unlocked only while a backup or a restore runs, and otherwise unplugged and stored away from the laptop. Keep
at least the last two sets. The BitLocker recovery key goes in the password manager, not on the laptop.

**Backup.** From an elevated PowerShell, with the drive unlocked (here `E:`):

```powershell
.\tools\admin\backup_sealed.ps1 -Destination E:\iap-backup
```

It asks for each partition account's password and, per partition: stops the service; has the partition's own
account write a manifest (`sealed_service.py backup-manifest`: the sha256 of every file and the audit chain head);
copies the partition with `robocopy /B /COPYALL`, which uses the administrator's backup privilege to copy bytes and
access lists without opening the files through the access check; has the account check the copy against the manifest
(`verify-backup`); and restarts the service. The set is a folder `iap-sealed-<UTC stamp>` holding the four copies
(each readable only by its account and SYSTEM, like the original), the four manifests and `backup_manifest.json`,
which records each manifest's sha256, file count and chain head. The console shows counts, chain heads and
robocopy's exit code, never file content. Write the stamp and the four chain heads in your log; a chain head can be
compared with the last line of `research/audit/CHECKPOINTS.jsonl` for that partition.

**Restore.** After a disk replacement, recreate the accounts and the services first
(`create_sealed_accounts.ps1`, `install_sealed_services.ps1`; new passwords are fine), move the empty new partitions
aside or delete them, then:

```powershell
.\tools\admin\restore_sealed.ps1 -BackupSet E:\iap-backup\iap-sealed-<stamp>
```

The script refuses before changing anything if the set has no `backup_manifest.json`, a manifest's sha256 differs
from the one the set recorded, or a target folder exists and is not empty (a live partition is refused too, since
the administrator cannot list it). It then recreates each partition with its access list, copies the files back with
their own access lists, and has each account run `verify-backup` on its restored partition: the audit chain must be
intact and its head and every file's sha256 must equal the manifest. Only then does it start that service. A
partition that fails stays stopped.

**Restore drill.** Every three months, and after any change to these scripts, restore the latest set into a scratch
folder instead of the live one:

```powershell
.\tools\admin\restore_sealed.ps1 -BackupSet E:\iap-backup\iap-sealed-<stamp> -SealedRoot D:\iap-restore-drill
```

A drill never stops or starts the live services. Pass means every partition reports `restored`. Afterwards delete
the drill folder from an elevated shell (`takeown /f D:\iap-restore-drill /r /a`, then remove it), and record the
date and result in your log. A drill that fails means the backups are not usable: take a new set before the next
ingestion.

The limit in section 4 applies here too: the administrator who can copy a partition with backup privilege can also
read the copy. The backup protects against losing the data, not against the owner.
