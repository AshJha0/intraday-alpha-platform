"""Multiple-testing ledger (spec §13: "control multiple testing and report
the number of experiments conducted").

Every evaluated alpha/configuration is one recorded experiment in
``research/experiments.json``.  The ledger is append-only within a run,
persisted as sorted JSON, and deliberately wall-clock-free (conventions §3:
reruns of the same code on the same data produce byte-identical ledgers).

**De-duplication (pinned, round-3).**  An experiment is identified by
``(alpha_id, kind, sha256 of the canonical config JSON)``.  Recording the
same identity again updates its result and does NOT increase the count:
re-running ``run_all.py`` five times is still one set of experiments, not
five.  Before this rule the Bonferroni denominator — and therefore the
"selection-adjusted" threshold every report and paper quotes — was a
function of how often a script had been run, which made the correction
meaningless.  A genuinely new configuration (a different threshold, a
different window) has a different config hash and is counted.

**Dataset scope (pinned, v1.4.0).**  A statistic computed on a different
dataset is a different look.  A ledger opened with ``dataset_version`` (the
content hash ``iap.experiment.tracker.data_version()``) stamps every entry
it records with that version and folds it into the identity, so re-running
a report pipeline after the dataset changed ADDS its looks instead of
overwriting the results of the same configuration on the old data.  The
looks of every earlier dataset stay in the file and in
``total_experiments``: the denominator only grows, and the corrected
thresholds only tighten.  An entry recorded before the rule keeps the key it
was given; ``research/migrate_ledger_dataset_scope.py`` stamped those with
the dataset they were run on.  The ``experiment_runner`` entries carry
``dataset_version`` inside their config (the ``ExperimentSpec``), which is
already part of their identity, so the runner opens the ledger unscoped and
its keys are unchanged.  The rendered file lists the datasets with their
entry and look counts (``datasets``, first-appearance order).

Multiple-testing math reported with every batch:

- Bonferroni: a per-test significance threshold ``alpha / n_experiments``
  (alpha = 0.05 pinned) and the |t| threshold it implies under a normal
  approximation.
- Deflated-Sharpe-style note: with n independent trials the expected
  maximum |t| under the global null grows like sqrt(2 ln n); any observed
  t-stat below that is consistent with pure selection.

**Gate look count (pinned, v1.5.0).**  Since v1.5.0 the PROMOTE t-stat gate
is derived from this ledger (``iap.validation.validate``,
``tstat_threshold="ledger"``): the threshold of a run is
``max(3.0, Bonferroni |t| at N looks)``.  ``N`` has to be a deterministic
function of the ledger state, and it must not depend on the order a pipeline
happens to loop over its alphas, nor change when the same run is repeated:

* for a NEW identity, ``N`` = the looks recorded before the run plus the
  looks the run itself adds — :meth:`ExperimentLedger.batch_total` of the
  identities the run is about to record.  A pipeline that validates 24
  alphas in one pass declares all 24 up front, so every alpha of the pass
  is judged at the same ``N``; the experiment runner declares its one
  experiment;
* the ``N`` a run was judged at is stored on its entry as ``gate_looks``;
* a RERUN of a recorded identity is judged at the recorded ``gate_looks``
  (:meth:`ExperimentLedger.gate_looks_for`), not at the ledger's later
  total — so repeating a run reproduces its verdict, and one experiment id
  holds one verdict.

The consequence is stated rather than hidden: the threshold is not
retroactive.  Looks made after a run tighten every later run; they do not
re-judge a recorded one.  A reader who wants the verdict under today's
denominator compares the recorded t with
:meth:`ExperimentLedger.bonferroni_t_threshold`.

**Concurrent writers (pinned).**  ``save`` is a locked read-modify-write:
under an exclusive lock file (``<ledger>.lock``, :mod:`iap.experiment.locking`)
it re-reads the file, replays every ``record`` call this instance made since
it last loaded or saved onto that fresh state, and replaces the file
atomically.  Two processes that each load, record and save therefore both
land — the second replays onto the first's result instead of overwriting it
with a stale snapshot — and a reader never sees a torn document.  With a
single writer the replay is the identity, so the bytes are exactly what the
unlocked implementation wrote.  ``record`` still returns the running total
of this instance's view; the total another writer raced in is visible after
``save`` (``total_experiments``).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path

from iap.experiment.locking import FileLock, atomic_write_text

PINNED_ALPHA = 0.05

#: ``x-version`` of ``research/experiments.json``: 2 since v1.4.0 (entries
#: carry ``dataset_version``; the document lists ``datasets``); 3 since
#: v1.5.0 (entries recorded under the ledger t-stat policy carry
#: ``gate_looks``, the look count their threshold was derived from).
LEDGER_X_VERSION = 3


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation,
    deterministic, |err| < 1.2e-8 — plenty for reporting thresholds)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        return -_norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


class ExperimentLedger:
    """Persistent experiment counter + entries (research/experiments.json)."""

    def __init__(
        self, path, lock_timeout_s: float = 30.0, dataset_version: str | None = None
    ) -> None:
        self.path = Path(path)
        self.lock_timeout_s = float(lock_timeout_s)
        #: Dataset scope of every ``record`` of this instance (module docs);
        #: ``None`` = unscoped (identity is alpha, kind and config alone).
        self.dataset_version = dataset_version
        self.entries: list[dict] = []
        self.total_experiments = 0
        self._index: dict[str, int] = {}
        #: ``record`` calls since the last load / save, replayed by ``save``
        #: onto the file's then-current content (see the module docs).
        self._pending: list[tuple[str, str, dict | None, dict | None, int, int | None]] = []
        self._load()

    def _load(self) -> None:
        """Reset the in-memory state to the file's content (empty if absent)."""
        self.entries = []
        self.total_experiments = 0
        self._index = {}
        if self.path.exists():
            blob = json.loads(self.path.read_text())
            self.entries = list(blob.get("entries", []))
            self.total_experiments = int(blob.get("total_experiments", len(self.entries)))
            for i, e in enumerate(self.entries):
                key = e.get("key") or self.experiment_key(
                    e.get("alpha_id", ""),
                    e.get("kind", ""),
                    e.get("config"),
                    e.get("dataset_version"),
                )
                self._index.setdefault(key, i)

    @staticmethod
    def experiment_key(
        alpha_id: str, kind: str, config: dict | None, dataset_version: str | None = None
    ) -> str:
        """Pinned experiment identity: alpha, kind and canonical config,
        plus the dataset when the ledger is dataset-scoped (module docs).
        Without ``dataset_version`` the payload — and the key — is exactly
        the pre-v1.4.0 one."""
        doc: dict = {"alpha_id": alpha_id, "kind": kind, "config": config or {}}
        if dataset_version is not None:
            doc["dataset_version"] = dataset_version
        payload = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def entry_dataset_version(entry: dict) -> str | None:
        """The dataset an entry was recorded on: its ``dataset_version``
        stamp, else the one inside its config (``experiment_runner``
        entries), else ``None`` (unknown)."""
        stamp = entry.get("dataset_version")
        if stamp is None:
            stamp = (entry.get("config") or {}).get("dataset_version")
        return None if stamp is None else str(stamp)

    def record(
        self,
        alpha_id: str,
        kind: str,
        config: dict | None = None,
        result: dict | None = None,
        count: int = 1,
        gate_looks: int | None = None,
    ) -> int:
        """Record ``count`` experiments (count > 1 = a declared batch, e.g. a
        horizon scan) and return the running total.  ``gate_looks`` is the
        look count the run's t-stat threshold was derived from (module docs,
        "Gate look count"); it is stored on a new entry and never replaces
        the value an existing entry already carries."""
        if count < 1:
            raise ValueError("count must be >= 1")
        if gate_looks is not None and int(gate_looks) < 1:
            raise ValueError("gate_looks must be >= 1")
        self._pending.append((alpha_id, kind, config, result, count, gate_looks))
        return self._apply(alpha_id, kind, config, result, count, gate_looks)

    def _apply(
        self,
        alpha_id: str,
        kind: str,
        config: dict | None,
        result: dict | None,
        count: int,
        gate_looks: int | None = None,
    ) -> int:
        key = self.experiment_key(alpha_id, kind, config, self.dataset_version)
        prev = self._index.get(key)
        if prev is not None:
            # Same identity: a rerun, not a new experiment (pinned).
            entry = self.entries[prev]
            entry["result"] = result or {}
            entry["reruns"] = int(entry.get("reruns", 0)) + 1
            if gate_looks is not None and "gate_looks" not in entry:
                entry["gate_looks"] = int(gate_looks)
            return self.total_experiments
        self.total_experiments += count
        self._index[key] = len(self.entries)
        entry = {
            "n": self.total_experiments,
            "key": key,
            "alpha_id": alpha_id,
            "kind": kind,
            "count": count,
            "config": config or {},
            "result": result or {},
        }
        if self.dataset_version is not None:
            entry["dataset_version"] = self.dataset_version
        if gate_looks is not None:
            entry["gate_looks"] = int(gate_looks)
        self.entries.append(entry)
        return self.total_experiments

    # -- multiple-testing report ----------------------------------------

    def bonferroni_threshold(self) -> float:
        n = max(self.total_experiments, 1)
        return PINNED_ALPHA / n

    def bonferroni_t_threshold(self) -> float:
        """|t| needed for two-sided significance at the Bonferroni level."""
        p = self.bonferroni_threshold() / 2.0
        return abs(_norm_ppf(p))

    @staticmethod
    def bonferroni_t_threshold_at(total_experiments: int) -> float:
        """The Bonferroni |t| threshold a ledger of ``total_experiments``
        looks implies (the formula of :meth:`bonferroni_t_threshold`, for a
        total that is not this instance's — e.g. the total a run WILL have
        once its own looks are debited)."""
        n = max(int(total_experiments), 1)
        return abs(_norm_ppf(PINNED_ALPHA / n / 2.0))

    def would_add(self, alpha_id: str, kind: str, config: dict | None, count: int) -> int:
        """Looks a ``record`` of this identity would add right now: ``count``
        for a new identity, 0 for a rerun (de-duplicated)."""
        key = self.experiment_key(alpha_id, kind, config, self.dataset_version)
        return 0 if key in self._index else int(count)

    def batch_total(self, identities: Sequence[tuple[str, str, dict | None, int]]) -> int:
        """The ledger total once a run has recorded ``identities`` —
        ``(alpha_id, kind, config, count)`` each: the looks recorded now plus
        the looks of every identity that is not in the ledger yet (a repeated
        identity inside the batch is counted once)."""
        total = self.total_experiments
        seen: set[str] = set()
        for alpha_id, kind, config, count in identities:
            key = self.experiment_key(alpha_id, kind, config, self.dataset_version)
            if key in self._index or key in seen:
                continue
            seen.add(key)
            total += int(count)
        return total

    def gate_looks_for(
        self, alpha_id: str, kind: str, config: dict | None, batch_total: int
    ) -> int:
        """The look count an identity's t-stat threshold is derived from
        (module docs, "Gate look count"): the ``gate_looks`` recorded on its
        entry when it has been run before, else ``batch_total``."""
        key = self.experiment_key(alpha_id, kind, config, self.dataset_version)
        prev = self._index.get(key)
        if prev is not None and self.entries[prev].get("gate_looks") is not None:
            return int(self.entries[prev]["gate_looks"])
        return int(batch_total)

    def expected_max_null_t(self) -> float:
        """Deflated-Sharpe-style yardstick: E[max |t|] under the global null
        with n independent trials ~ sqrt(2 ln n)."""
        n = max(self.total_experiments, 2)
        return math.sqrt(2.0 * math.log(n))

    @property
    def distinct_experiments(self) -> int:
        """Distinct (alpha, kind, config[, dataset]) identities recorded."""
        return len(self._index)

    def datasets(self) -> list[dict]:
        """Entries and looks per dataset, in first-appearance order
        (``dataset_version`` ``None`` = recorded without a dataset stamp)."""
        out: dict[str | None, dict] = {}
        for e in self.entries:
            version = self.entry_dataset_version(e)
            row = out.setdefault(version, {"dataset_version": version, "entries": 0, "looks": 0})
            row["entries"] += 1
            row["looks"] += int(e.get("count", 1))
        return list(out.values())

    def note(self) -> str:
        return (
            f"Multiple testing: {self.total_experiments} experiments recorded. "
            f"Bonferroni per-test p-threshold {self.bonferroni_threshold():.2e} "
            f"(|t| >= {self.bonferroni_t_threshold():.2f}); deflated-Sharpe-style "
            f"selection yardstick: expected max |t| under the global null is "
            f"~{self.expected_max_null_t():.2f} — any t below that is consistent "
            f"with pure selection over this many trials."
        )

    def _render(self) -> str:
        blob = {
            "x-version": LEDGER_X_VERSION,
            "description": (
                "Multiple-testing ledger (spec §13). Deterministic: no "
                "wall-clock; identical rerun => identical file. Experiments "
                "are de-duplicated by (alpha_id, kind, canonical config, "
                "dataset_version): re-running the same script on the same "
                "dataset does not inflate the Bonferroni denominator; the "
                "same configuration on a new dataset is a new look, and the "
                "looks of earlier datasets are kept (`datasets`). An entry "
                "judged under the ledger t-stat policy carries `gate_looks`, "
                "the look count its threshold was derived from."
            ),
            "datasets": self.datasets(),
            "distinct_experiments": self.distinct_experiments,
            "total_experiments": self.total_experiments,
            "pinned_alpha": PINNED_ALPHA,
            "bonferroni_p_threshold": self.bonferroni_threshold(),
            "bonferroni_t_threshold": self.bonferroni_t_threshold(),
            "expected_max_null_t": self.expected_max_null_t(),
            "entries": self.entries,
        }
        return json.dumps(blob, indent=2, sort_keys=True) + "\n"

    def save(self) -> None:
        """Locked read-modify-write (see the module docs): re-read the file,
        replay this instance's pending ``record`` calls onto it, replace the
        file atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path, timeout_s=self.lock_timeout_s):
            pending, self._pending = self._pending, []
            self._load()
            for op in pending:
                self._apply(*op)
            atomic_write_text(self.path, self._render())
