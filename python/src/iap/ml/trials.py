"""The trial ledger (ML protocol section 9): every fit, chain-hashed, under the protocol hash, within budget.

A ledger is a JSON file ``{"protocol_sha256", "entries", "chain_head"}``.  The
first entry is always ``protocol_freeze`` with the hash the fits run under; a
ledger without it refuses fits.  Each ``fit`` entry names the family, tier,
class, group, hyper-parameters, split, seed and complexity; ``evaluation``
entries attach admission statistics to a fit; ``decision`` entries record the
tier's advancement and the carried candidate.  The search budget per tier
(protocol section 4) is enforced at ``record_fit``: the fit beyond the budget is
refused with :class:`BudgetExceeded` and the refusal is itself recorded, so the
count of attempts is in the ledger.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from iap.contracts.versions import canonical_json
from iap.ml.protocol import tier_of

__all__ = ["BudgetExceeded", "TrialLedger", "TrialLedgerError"]


class TrialLedgerError(ValueError):
    """The ledger is malformed, tampered, or used before the protocol freeze."""


class BudgetExceeded(TrialLedgerError):
    """A fit beyond the tier's search budget."""


def _link(prev: str, entry: dict[str, Any]) -> str:
    return hashlib.sha256((prev + canonical_json(entry)).encode("ascii")).hexdigest()


class TrialLedger:
    def __init__(self, path: str | Path, protocol: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.entries: list[dict[str, Any]] = []
        self.protocol_sha256: str | None = None
        self._budgets: dict[str, int] = {}
        if protocol is not None:
            self._budgets = {t: int(v["budget"]) for t, v in protocol["tiers"].items()}
        if self.path.exists():
            blob = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = list(blob.get("entries", []))
            self.protocol_sha256 = blob.get("protocol_sha256")
            if blob.get("chain_head") != self.chain_head:
                raise TrialLedgerError(
                    f"{self.path}: chain head does not match the entries (edited or reordered)"
                )
            if self.entries and self.entries[0].get("kind") != "protocol_freeze":
                raise TrialLedgerError("the first entry of a trial ledger is the protocol freeze")

    # ------------------------------------------------------------------ chain
    @property
    def chain_head(self) -> str:
        h = "0" * 64
        for e in self.entries:
            h = _link(h, e)
        return h

    def _append(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry = dict(entry)
        entry["seq"] = len(self.entries)
        self.entries.append(entry)
        return entry

    # ------------------------------------------------------------------ entries
    def freeze(self, protocol_sha256: str, revision: int) -> dict[str, Any]:
        """The first entry: the protocol hash every later fit runs under."""
        if self.entries:
            raise TrialLedgerError("the protocol freeze is the first entry and is recorded once")
        self.protocol_sha256 = protocol_sha256
        return self._append(
            {"kind": "protocol_freeze", "protocol_sha256": protocol_sha256, "revision": revision}
        )

    def _require_frozen(self) -> None:
        if not self.entries or self.entries[0].get("kind") != "protocol_freeze":
            raise TrialLedgerError("no fit before the protocol freeze entry")

    def fits(self, tier: str | None = None, family_id: str | None = None) -> list[dict[str, Any]]:
        out = [e for e in self.entries if e["kind"] in ("fit", "fit_refused")]
        if tier is not None:
            out = [e for e in out if e["tier"] == tier]
        if family_id is not None:
            out = [e for e in out if e["family_id"] == family_id]
        return out

    def record_fit(
        self,
        *,
        family_id: str,
        model_class: str,
        group_id: str,
        hyper: dict[str, Any],
        split: dict[str, Any],
        seed: int,
        complexity: float,
        fit_sha256: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One fit; refused (and the refusal recorded) beyond the tier's budget for the family."""
        self._require_frozen()
        tier = tier_of(model_class)
        budget = self._budgets.get(tier)
        used = len(self.fits(tier, family_id))
        base = {
            "family_id": family_id,
            "tier": tier,
            "model_class": model_class,
            "group_id": group_id,
            "hyper": dict(hyper),
            "split": dict(split),
            "seed": int(seed),
            "protocol_sha256": self.protocol_sha256,
        }
        if budget is not None and used >= budget:
            self._append(
                {
                    **base,
                    "kind": "fit_refused",
                    "reason": f"search budget {budget} of tier {tier} exhausted",
                }
            )
            raise BudgetExceeded(f"{family_id} {tier}: {used} fits recorded, budget {budget}")
        return self._append(
            {
                **base,
                "kind": "fit",
                "complexity": float(complexity),
                "fit_sha256": fit_sha256,
                **(extra or {}),
            }
        )

    def record_evaluation(self, fit_seq: int, admission: dict[str, Any]) -> dict[str, Any]:
        self._require_frozen()
        if fit_seq >= len(self.entries) or self.entries[fit_seq]["kind"] != "fit":
            raise TrialLedgerError(f"entry {fit_seq} is not a fit")
        return self._append(
            {"kind": "evaluation", "fit_seq": fit_seq, "admission": dict(admission)}
        )

    def record_decision(
        self, tier: str, family_id: str, decision: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_frozen()
        return self._append(
            {"kind": "decision", "tier": tier, "family_id": family_id, **dict(decision)}
        )

    def record_note(self, kind: str, **fields: Any) -> dict[str, Any]:
        """A non-fit entry (block length fixed, delta derived) that must precede the comparisons."""
        self._require_frozen()
        return self._append({"kind": kind, **fields})

    def check_complete(
        self, required: tuple[str, ...] = ("config_sha256", "data_partition", "seed", "fit_sha256")
    ) -> dict[str, Any]:
        """Refuse a ledger in which a fit lacks a required field, an evaluation or a tier decision.

        Every ``fit`` must carry ``required`` and be evaluated exactly once
        (a fit is never silently dropped from the comparison); every tier with a
        fit must close with a ``decision`` for that family.
        """
        self._require_frozen()
        fits = [e for e in self.entries if e["kind"] == "fit"]
        evaluated: dict[int, int] = {}
        for e in self.entries:
            if e["kind"] == "evaluation":
                evaluated[e["fit_seq"]] = evaluated.get(e["fit_seq"], 0) + 1
        problems = []
        for f in fits:
            missing = [k for k in required if f.get(k) in (None, "")]
            if missing:
                problems.append(f"fit {f['seq']} lacks {missing}")
            if evaluated.get(f["seq"], 0) != 1:
                problems.append(f"fit {f['seq']} evaluated {evaluated.get(f['seq'], 0)} times")
        decided = {(e["tier"], e["family_id"]) for e in self.entries if e["kind"] == "decision"}
        for tier, fam in sorted({(f["tier"], f["family_id"]) for f in fits}):
            if (tier, fam) not in decided:
                problems.append(f"{fam} {tier} has fits but no decision")
        if problems:
            raise TrialLedgerError("trial ledger incomplete: " + "; ".join(problems))
        return {"fits": len(fits), "decisions": len(decided)}

    # ------------------------------------------------------------------ persistence
    def to_document(self) -> dict[str, Any]:
        return {
            "protocol_sha256": self.protocol_sha256,
            "entries": self.entries,
            "chain_head": self.chain_head,
            "n_fits": len([e for e in self.entries if e["kind"] == "fit"]),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
