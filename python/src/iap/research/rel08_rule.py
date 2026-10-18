"""The REL-08 continuation rule as a signed JSON document (v1.18 REL-08, programme 9.5).

``research/v118/REL08_CONTINUATION_RULE.md`` is the human text of the rule;
``research/v118/rel08_rule_DRAFT.json`` is its machine copy: every number the
screen (:mod:`iap.research.rel08_screen`) decides with — the variant list,
book economics, sufficiency floors, bootstrap, thresholds C1-C8 and the
decision map's cap — lives there and nowhere else in code.  The draft binds
the markdown by ``rule_document_sha256`` (LF-normalised), so an edit of either
file without the other is a mismatch.

Signing (``tools/rel08_rule_sign.py``) copies the draft to
``research/v118/rel08_rule_signed.json``, adds the REL-02a
``analysis_identity`` block (screen scripts, ``python/src/iap`` tree hash,
commit, dataset version, family, metrics, thresholds) and an Ed25519
signature under the trust-root maintainer key.

:func:`load_signed_rule` refuses, naming the reason:

* a document that is not signed, or whose signature does not verify under
  ``research/agents/trust_roots.json``;
* a body that fails :func:`validate_rule` (the schema below);
* a signed body that differs from the committed draft (other than the
  signing fields), or a draft whose ``rule_document_sha256`` no longer
  matches the markdown;
* a variant list that is not the registered one (:data:`REGISTERED_VARIANTS`,
  the markdown's section 9 table: a guard, not a source of numbers).

The analysis identity is checked separately, at run time, by
:func:`check_identity` (it needs the dataset version and a git checkout).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
SCHEMA = "iap.rel08_rule/v1"
KIND = "rel08_continuation_rule"
RULE_MD_REL = "research/v118/REL08_CONTINUATION_RULE.md"
DRAFT_REL = "research/v118/rel08_rule_DRAFT.json"
SIGNED_REL = "research/v118/rel08_rule_signed.json"
#: the ledger kind and family of every look (rule section 7)
FAMILY = "rel08_screen"
#: the statistics the signed identity binds (``analysis_identity.metrics``)
METRICS = (
    "rank_ic_mean",
    "rank_ic_lower",
    "net_bps_mean",
    "net_bps_lower",
    "net_bps_se",
    "mde_bps",
    "ess",
    "conservative_net_bps",
    "influential_removed_net_bps",
    "alt_normalisation_net_bps",
)
#: fields added by signing (absent from the draft)
SIGNING_FIELDS = ("signed", "signature", "analysis_identity", "signed_by", "signed_utc")
#: the scripts the signature binds
BOUND_SCRIPTS = (
    "python/src/iap/research/rel08_rule.py",
    "python/src/iap/research/rel08_screen.py",
    "tools/rel08_rule_sign.py",
)

#: The registered list (markdown section 9): (id, score, horizon_min, rebalance_min, book).
#: A guard against a JSON edit; the numbers that decide are read from the JSON.
REGISTERED_VARIANTS: tuple[tuple[str, str, int, int, str], ...] = (
    ("V1", "vwap_deviation", 15, 15, "market_neutral"),
    ("V2", "vwap_deviation", 15, 15, "sector_neutral"),
    ("V3", "vwap_deviation", 30, 30, "market_neutral"),
    ("V4", "vwap_deviation", 30, 30, "sector_neutral"),
    ("V5", "residual_reversion", 15, 15, "market_neutral"),
    ("V6", "residual_reversion", 15, 15, "sector_neutral"),
    ("V7", "residual_reversion", 30, 30, "market_neutral"),
    ("V8", "residual_reversion", 30, 30, "sector_neutral"),
    ("V9", "etf_catch_up", 1, 5, "market_neutral"),
    ("V10", "etf_catch_up", 3, 5, "market_neutral"),
    ("V11", "etf_catch_up", 5, 5, "market_neutral"),
)
SCORES = ("vwap_deviation", "residual_reversion", "etf_catch_up")
BOOKS = ("market_neutral", "sector_neutral")


class RuleRefused(ValueError):
    """The rule document may not be used (unsigned, invalid or mismatched)."""


# JSON schema of the rule body (draft-07 subset; :func:`validate_rule` enforces it
# without a jsonschema dependency).
_NUM = {"type": "number"}
_INT = {"type": "integer"}
RULE_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": SCHEMA,
    "type": "object",
    "required": [
        "schema", "kind", "rule_document", "rule_document_sha256", "session", "dataset",
        "label", "family", "variants", "book", "costs", "sufficiency", "statistics",
        "thresholds", "decision", "implementation",
    ],
    "properties": {
        "schema": {"const": SCHEMA},
        "kind": {"const": KIND},
        "rule_document": {"const": RULE_MD_REL},
        "rule_document_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "session": {"type": "string"},
        "label": {"type": "string"},
        "dataset": {
            "type": "object",
            "required": ["label", "dataset_version", "feature_identity"],
        },
        "family": {
            "type": "object",
            "required": ["name", "K", "alpha"],
            "properties": {"name": {"const": FAMILY}, "K": _INT, "alpha": _NUM},
        },
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "score", "horizon_min", "rebalance_min", "book"],
            },
        },
        "book": {
            "type": "object",
            "required": [
                "notional", "gross_cap", "participation_cap", "w_max", "turnover_cap",
                "impact", "exec_delay_s", "max_age_s", "flatten_at_session_end", "weights",
                "alpha_bps", "risk_aversion", "cov_lam", "cov_warmup_min", "solver_iters",
            ],
        },
        "costs": {
            "type": "object",
            "required": ["conservative_spread_mult", "conservative_fee_mult",
                         "conservative_impact_mult"],
        },
        "sufficiency": {
            "type": "object",
            "required": ["names_per_rebalance_min", "score_max_age_s", "usable_rebalances_min",
                         "names_usable_min", "names_usable_frac", "max_gap_min"],
        },
        "statistics": {
            "type": "object",
            "required": ["bootstrap", "block_minutes", "draws", "seed", "mde_z_sum",
                         "ess_inflation_floor"],
        },
        "thresholds": {
            "type": "object",
            "required": ["C2_mde_max_bps", "C3_net_min_bps", "C3_lower_min_bps", "C4_ess_min",
                         "C5_retention", "C6_retention", "C6_names_removed", "C7_retention",
                         "C8_ic_min", "C8_lower_min"],
        },
        "decision": {"type": "object", "required": ["max_continue", "rank_by"]},
        "implementation": {"type": "object"},
    },
}  # fmt: skip


def _norm_sha_text(path: Path) -> str:
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rule_document_sha256(root: Path | None = None) -> str:
    """LF-normalised sha256 of the markdown rule."""
    return _norm_sha_text(Path(root or REPO) / RULE_MD_REL)


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def validate_rule(doc: Mapping[str, Any]) -> None:
    """Check ``doc`` against :data:`RULE_JSON_SCHEMA` and the rule's own
    consistency (K = len(variants), the registered list, value ranges)."""
    bad: list[str] = []
    props = RULE_JSON_SCHEMA["properties"]
    for key in RULE_JSON_SCHEMA["required"]:
        if key not in doc:
            bad.append(f"missing {key}")
    for key, spec in props.items():
        if key not in doc:
            continue
        v = doc[key]
        if "const" in spec and v != spec["const"]:
            bad.append(f"{key} must be {spec['const']!r}")
        if spec.get("type") == "object":
            if not isinstance(v, Mapping):
                bad.append(f"{key} must be an object")
                continue
            for sub in spec.get("required", []):
                if sub not in v:
                    bad.append(f"missing {key}.{sub}")
            for sub, s2 in spec.get("properties", {}).items():
                if sub in v and "const" in s2 and v[sub] != s2["const"]:
                    bad.append(f"{key}.{sub} must be {s2['const']!r}")
    if bad:
        raise RuleRefused("rule schema: " + "; ".join(bad))
    th = doc["thresholds"]
    for k, v in th.items():
        if not _is_num(v):
            bad.append(f"thresholds.{k} must be a number")
    for sect in ("costs", "sufficiency", "statistics", "book"):
        for k, v in doc[sect].items():
            vals = list(v.values()) if isinstance(v, Mapping) else [v]
            for x in vals:
                if isinstance(x, bool | str) or x is None:
                    continue
                if not _is_num(x) or x < 0:
                    bad.append(f"{sect}.{k} must be a non-negative number")
    floors = doc["sufficiency"]["usable_rebalances_min"]
    for v in doc.get("variants", []):
        if not isinstance(floors, Mapping) or str(v.get("rebalance_min")) not in floors:
            bad.append(f"sufficiency.usable_rebalances_min has no floor for {v.get('id')}")
            break
    variants = doc["variants"]
    got = tuple(
        (v.get("id"), v.get("score"), v.get("horizon_min"), v.get("rebalance_min"), v.get("book"))
        for v in variants
    )
    if got != REGISTERED_VARIANTS:
        bad.append("variants differ from the registered list (rule section 9)")
    if doc["family"]["K"] != len(variants):
        bad.append(f"family.K {doc['family']['K']} != {len(variants)} variants")
    if not (0 < doc["family"]["alpha"] < 1):
        bad.append("family.alpha must lie in (0, 1)")
    if doc["decision"]["rank_by"] != "net_bps_lower":
        bad.append("decision.rank_by must be 'net_bps_lower'")
    st = doc["statistics"]
    if st.get("bootstrap") != "moving_block":
        bad.append("statistics.bootstrap must be 'moving_block'")
    if bad:
        raise RuleRefused("rule schema: " + "; ".join(bad))


@dataclass(frozen=True)
class Rule:
    """A loaded rule.  ``doc`` is the full document; the accessors are views."""

    doc: Mapping[str, Any]
    signed: bool
    path: str | None = None

    @property
    def K(self) -> int:  # noqa: N802 - the rule's name for it
        return int(self.doc["family"]["K"])

    @property
    def level(self) -> float:
        """One-sided lower-bound level ``1 - alpha / K`` (Bonferroni)."""
        return 1.0 - float(self.doc["family"]["alpha"]) / self.K

    @property
    def variants(self) -> list[Mapping[str, Any]]:
        return list(self.doc["variants"])

    @property
    def thresholds(self) -> Mapping[str, Any]:
        return self.doc["thresholds"]

    def section(self, name: str) -> Mapping[str, Any]:
        return self.doc[name]

    @property
    def message_sha256(self) -> str | None:
        sig = self.doc.get("signature")
        return sig.get("message_sha256") if isinstance(sig, Mapping) else None


def draft_body(doc: Mapping[str, Any]) -> dict[str, Any]:
    """``doc`` without the fields signing adds."""
    return {k: v for k, v in doc.items() if k not in SIGNING_FIELDS}


def load_draft(root: Path | None = None, path: Path | None = None) -> Rule:
    """The UNSIGNED draft (for tests, previews and the sign tool; the screen
    refuses it).  Checks the schema and the markdown binding."""
    r = Path(root or REPO)
    p = Path(path) if path is not None else r / DRAFT_REL
    doc = json.loads(p.read_text(encoding="utf-8"))
    validate_rule(doc)
    if doc.get("signed"):
        raise RuleRefused(f"{p}: a draft must not carry signing fields")
    want = rule_document_sha256(r)
    if doc["rule_document_sha256"] != want:
        raise RuleRefused(
            f"{p}: rule_document_sha256 {doc['rule_document_sha256'][:12]} does not match "
            f"{RULE_MD_REL} ({want[:12]}): the markdown and its JSON copy disagree"
        )
    return Rule(doc=doc, signed=False, path=str(p))


def load_signed_rule(
    path: Path | str | None = None,
    root: Path | None = None,
    *,
    draft_path: Path | str | None = None,
) -> Rule:
    """The signed rule; :class:`RuleRefused` on anything listed in the module docs."""
    r = Path(root or REPO)
    p = Path(path) if path is not None else r / SIGNED_REL
    if not p.is_file():
        raise RuleRefused(f"{p}: no signed rule (sign {DRAFT_REL} with tools/rel08_rule_sign.py)")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("signed") is not True or not isinstance(doc.get("signature"), Mapping):
        raise RuleRefused(f"{p}: the rule is not signed; the screen refuses an unsigned rule")
    from iap.agents.analysis_identity import SignatureError, verify_signed_document

    try:
        verify_signed_document(r, doc)
    except SignatureError as exc:
        raise RuleRefused(f"{p}: {exc}") from exc
    validate_rule(doc)
    if not isinstance(doc.get("analysis_identity"), Mapping):
        raise RuleRefused(f"{p}: the signed rule binds no analysis_identity")
    draft = load_draft(r, Path(draft_path) if draft_path is not None else None)
    if draft_body(doc) != dict(draft.doc):
        raise RuleRefused(f"{p}: the signed body differs from {DRAFT_REL} (mismatched copy)")
    return Rule(doc=doc, signed=True, path=str(p))


def sign_rule(
    draft: Mapping[str, Any],
    private_hex: str,
    maintainer: str,
    identity: Mapping[str, Any],
    signed_by: str,
    signed_utc: str,
) -> dict[str, Any]:
    """The signed copy of ``draft``: ``analysis_identity`` and provenance added,
    Ed25519 over ``canonical(document without 'signature')`` (the REL-02a
    prereg convention, verified by ``verify_signed_document``)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from iap.agents import signing
    from iap.agents.blackboard import canonical

    doc = draft_body(draft)
    doc.update(
        signed=True,
        analysis_identity=dict(identity),
        signed_by=signed_by,
        signed_utc=signed_utc,
    )
    msg = canonical(doc).encode("ascii")
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex)).sign(msg).hex()
    pub = signing.public_of(private_hex)
    doc["signature"] = {
        "scheme": "ed25519",
        "maintainer": maintainer,
        "public_key": pub,
        "trust_root": "research/agents/trust_roots.json",
        "message": "iap.agents.blackboard.canonical(this document without 'signature')",
        "message_sha256": hashlib.sha256(msg).hexdigest(),
        "sig": sig,
    }
    return doc


def render(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def check_identity(rule: Rule, root: Path | None, dataset_version: str) -> dict[str, Any]:
    """REL-02a: the code, commit and dataset about to run are those the
    signature bound.  :class:`RuleRefused` lists every mismatch."""
    from iap.agents.analysis_identity import IdentityError, verify_identity

    if not rule.signed:
        raise RuleRefused("check_identity needs a signed rule")
    try:
        return verify_identity(
            Path(root or REPO),
            rule.doc.get("analysis_identity"),
            data_versions=[dataset_version],
            family=FAMILY,
            metrics=METRICS,
            thresholds=dict(rule.thresholds),
        )
    except IdentityError as exc:
        raise RuleRefused(str(exc)) from exc
