"""Hash-locked holdout registry (v1.18 REL-02b, review R-G).

``research/holdout_registry.json`` records, for every dataset the project
has ingested and for every (session, symbol) scope of the 2025/26 holdout,
whether it is

* ``in_sample``: free to read;
* ``spent``: a pre-registered confirmatory look already read it
  (``spent_by`` lists the prereg ids); it cannot be read again for a new
  analysis, and a replay needs a signed prereg id listed in ``spent_by``;
* ``unread``: reserved for a registered confirmatory run
  (``reserved_for`` lists the signed prereg documents allowed to read it).

Identity is **content**, not a path.  :func:`dataset_identity` hashes the
dataset's normalized files and compares them with ``dataset.json``; the
registry is looked up by that content hash and ``dataset_version``, and
independently by (session date, symbol) scope, so a renamed directory, a
symlink, a copied tree, a re-ingest of the same session or a date label
picked out of a multi-session dataset resolves to the same state.  The old
``"holdout" in path`` substring checks are gone: a dataset called
``fresh`` that holds 2026-05-15 is refused, one called ``holdout_mock`` that
is synthetic is not.

Every supported data-loading entry point calls :func:`guard` before it reads
a byte of data.  ``guard`` fails closed: a real dataset the registry does
not know is refused too.  Each attempt, allowed or refused, appends one
JSON line to ``data/real/holdout_access.jsonl`` (git-ignored with the data).

The registry body carries ``body_sha256``; :func:`load_registry` refuses a
file whose body no longer matches, so an edit must go through
``python -m iap.research.holdout_registry seal`` and shows in review.  What
code cannot enforce: someone who edits the registry AND reseals it, or who
deletes a file's hash check, is stopped only by review (CODEOWNERS) and by
CI pinning the sealed hash (``tests/test_holdout_registry.py``).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
REGISTRY_REL = "research/holdout_registry.json"
LOG_REL = "data/real/holdout_access.jsonl"
#: optional override of the access-log path (tests); it cannot weaken a check
LOG_ENV = "IAP_HOLDOUT_ACCESS_LOG"
SCHEMA = "iap.holdout_registry/v1"
STATES = ("in_sample", "spent", "unread")
_SEVERITY = {"in_sample": 0, "spent": 1, "unread": 2}


class HoldoutRefused(ValueError):
    """The dataset (or a selected session / symbol of it) may not be read."""


# ----------------------------------------------------------- registry file --
def _canonical(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def body_sha256(doc: Mapping[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "body_sha256"}
    return hashlib.sha256(_canonical(body).encode("ascii")).hexdigest()


def seal(doc: dict[str, Any]) -> dict[str, Any]:
    doc = {k: v for k, v in doc.items() if k != "body_sha256"}
    doc["body_sha256"] = body_sha256(doc)
    return doc


def render(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def load_registry(path: Path | str | None = None, root: Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else Path(root or REPO) / REGISTRY_REL
    if not p.is_file():
        raise HoldoutRefused(f"{p}: holdout registry missing; every real dataset read fails closed")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise HoldoutRefused(f"{p}: unknown registry schema {doc.get('schema')!r}")
    if doc.get("body_sha256") != body_sha256(doc):
        raise HoldoutRefused(
            f"{p}: body_sha256 does not match the content (edited without `seal`); refusing"
        )
    return doc


# -------------------------------------------------------- dataset identity --
def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(files: Mapping[str, str]) -> str:
    """sha256 over ``name \\n sha256 \\n`` of the normalized files, sorted by name
    (the same recipe as ``iap.experiment.tracker.data_version``)."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\n" + files[name].encode() + b"\n")
    return h.hexdigest()


def dataset_root(path: Path | str) -> Path:
    """The dataset a path belongs to: ``path`` itself or the nearest ancestor (up to
    three levels: ``features/``, ``normalized/``, ``configs/instruments``) that holds a
    ``dataset.json``.  Symlinks are resolved first."""
    p = Path(path).resolve()
    for cand in (p, *list(p.parents)[:3]):
        if (cand / "dataset.json").is_file():
            return cand
    return p


def dataset_identity(dataset_dir: Path | str, *, rehash: bool = True) -> dict[str, Any]:
    """What the directory *is*: kind, declared version, sessions, symbols and the
    content hash of its normalized files.

    ``rehash`` re-reads the normalized files (``dataset.json`` -> ``normalized.files``)
    and refuses when a byte differs from the manifest.  A dataset whose
    normalized files are no longer on disk (features-only copies) is reported
    with ``content_verified: False`` and the manifest's own hashes."""
    d = dataset_root(dataset_dir)  # resolves symlinks: the real tree is hashed
    man_path = d / "dataset.json"
    if not man_path.is_file():
        raise HoldoutRefused(f"{dataset_dir}: no dataset.json; an unregistered dataset is refused")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    declared = dict((man.get("normalized") or {}).get("files") or {})
    verified = False
    if declared:
        norm = d / "normalized"
        present = [n for n in declared if (norm / n).is_file()]
        if rehash and present:
            for n in present:
                actual = _sha256_file(norm / n)
                if actual != declared[n]:
                    raise HoldoutRefused(
                        f"{dataset_dir}: {n} content differs from dataset.json (tampered or "
                        "re-written); refusing"
                    )
            verified = len(present) == len(declared)
    sessions = sorted((man.get("sessions") or {}).keys())
    symbols = sorted(str(u.get("symbol")) for u in man.get("universe") or [] if u.get("symbol"))
    return {
        "kind": man.get("kind"),
        "dataset_version": man.get("dataset_version"),
        "content_hash": content_hash(declared) if declared else None,
        "sessions": sessions,
        "symbols": symbols,
        "content_verified": verified,
    }


# ------------------------------------------------------------------- guard --
def _rule_for(rules: Sequence[Mapping[str, Any]], symbol: str) -> Mapping[str, Any] | None:
    for r in rules:
        syms = r.get("symbols", "*")
        if syms == "*" or symbol in syms:
            return r
    return None


def _scope_decisions(
    reg: Mapping[str, Any], sessions: Iterable[str], symbols: Iterable[str]
) -> list[dict[str, Any]]:
    out = []
    index = reg.get("sessions", {})
    for s in sessions:
        rules = index.get(s)
        for sym in symbols:
            r = _rule_for(rules, sym) if rules else None
            out.append(
                {
                    "session": s,
                    "symbol": sym,
                    "state": r["state"] if r else "unregistered",
                    "rule": r,
                }
            )
    return out


def _log(path: Path | None, record: dict[str, Any], must: bool) -> None:
    p = Path(path) if path else Path(os.environ.get(LOG_ENV) or REPO / LOG_REL)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        if must:
            raise HoldoutRefused(f"access log {p} not writable ({exc}); refusing") from exc


def refuse_unit_name(name: str, *, root: Path | None = None, registry=None) -> None:
    """For callers that only hold a cache / unit NAME (``ds3_20190130_AAPL.X.npy``,
    ``r7_20251128``): refuse when it carries a session date (YYYYMMDD) or a dataset
    label that the registry does not hold in-sample.  A date the registry does not
    know is refused too (fail closed)."""
    import re

    reg = registry if isinstance(registry, Mapping) else load_registry(registry, root)
    labels = {e.get("label"): e for e in reg.get("datasets", {}).values()}
    for label, entry in labels.items():
        if entry["state"] != "in_sample" and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(str(label))}(?![A-Za-z0-9])", Path(name).name
        ):
            raise HoldoutRefused(f"{name}: dataset {label} is {entry['state']} (holdout registry)")
    for tok in re.findall(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", name):
        iso = "-".join(tok)
        rules = reg.get("sessions", {}).get(iso)
        if rules is None or any(r["state"] != "in_sample" for r in rules):
            raise HoldoutRefused(
                f"{name}: session {iso} is spent / unread / unregistered in the holdout registry"
            )


def guard_or_exit(
    dataset_dir: Path | str, purpose: str, prereg_id: object = None
) -> dict[str, Any]:
    """:func:`guard` for CLI entry points: a refusal becomes ``SystemExit(message)``.
    ``prereg_id`` is used only when it is a string (a legacy ``allow_holdout`` bool
    authorises nothing)."""
    try:
        return guard(
            dataset_dir,
            purpose=purpose,
            prereg_id=prereg_id if isinstance(prereg_id, str) else None,
        )
    except HoldoutRefused as exc:
        raise SystemExit(str(exc)) from exc


def guard_session_label(
    dataset_dir: Path | str, label: str | None, purpose: str, prereg_id: object = None
) -> dict[str, Any]:
    """:func:`guard_or_exit` for a caller that selects one session by a ``YYYYMMDD`` /
    ISO label out of a (possibly multi-session) dataset: only that session is
    checked, but it is checked against the registry, not the manifest's own label."""
    sessions = None
    if label:
        digits = "".join(c for c in label if c.isdigit())
        if len(digits) >= 8:
            d = digits[-8:]
            sessions = [f"{d[:4]}-{d[4:6]}-{d[6:]}"]
    try:
        return guard(
            dataset_dir,
            purpose=purpose,
            sessions=sessions,
            prereg_id=prereg_id if isinstance(prereg_id, str) else None,
        )
    except HoldoutRefused as exc:
        raise SystemExit(str(exc)) from exc


def prereg_is_signed(prereg_id: str, root: Path | None = None) -> bool:
    """A prereg id counts when it is (a) a signed-off prereg (format 3) on the
    blackboard, or (b) the repo-relative path of a prereg document whose Ed25519
    signature verifies under the trust root."""
    r = Path(root or REPO)
    doc_path = r / prereg_id
    if prereg_id.endswith(".json") and doc_path.is_file():
        from iap.agents.analysis_identity import SignatureError, verify_signed_document

        try:
            verify_signed_document(r, json.loads(doc_path.read_text(encoding="utf-8")))
            return True
        except (SignatureError, ValueError):
            return False
    from iap.agents.blackboard import Blackboard
    from iap.agents.prereg_gate import board_path
    from iap.agents.workflow import PREREG_FORMAT_SIGNED_OFF

    board = board_path(r)
    if not board.is_file():
        return False
    bb = Blackboard(board)
    return any(
        e["kind"] == "prereg"
        and e["body"].get("prereg_id") == prereg_id
        and e["body"].get("format") == PREREG_FORMAT_SIGNED_OFF
        for e in bb.entries()
    )


def guard(
    dataset_dir: Path | str,
    *,
    purpose: str,
    prereg_id: str | None = None,
    sessions: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
    root: Path | None = None,
    registry: Mapping[str, Any] | Path | None = None,
    log_path: Path | None = None,
    rehash: bool = True,
) -> dict[str, Any]:
    """Allow or refuse a read of ``dataset_dir`` (all of it, or the selected
    ``sessions`` / ``symbols``).  Returns the decision record; raises
    :class:`HoldoutRefused` otherwise.  Always logs the attempt."""
    rec: dict[str, Any] = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "dir": str(dataset_dir),
        "purpose": purpose,
        "prereg_id": prereg_id,
    }
    try:
        decision = _decide(dataset_dir, prereg_id, sessions, symbols, root, registry, rehash, rec)
    except HoldoutRefused as exc:
        rec.update(decision="refused", reason=str(exc))
        _log(log_path, rec, must=False)
        raise
    rec.update(decision="allowed", **decision)
    _log(log_path, rec, must=decision.get("state") != "in_sample")
    return rec


def _decide(
    dataset_dir, prereg_id, sessions, symbols, root, registry, rehash, rec
) -> dict[str, Any]:
    ident = dataset_identity(dataset_dir, rehash=rehash)
    rec.update(
        dataset_version=ident["dataset_version"],
        content_hash=ident["content_hash"],
        content_verified=ident["content_verified"],
    )
    if ident["kind"] != "real":
        return {"state": "synthetic", "reason": "kind != real"}
    reg = registry if isinstance(registry, Mapping) else load_registry(registry, root)
    known = None
    for key, entry in reg.get("datasets", {}).items():
        if key == ident["dataset_version"] or (
            ident["content_hash"] and entry.get("content_hash") == ident["content_hash"]
        ):
            known = entry
            break
    # a known content hash fixes the sessions and symbols: the manifest's labels
    # are a claim, the registry's are the record (date-label / rename bypass)
    all_sessions = list(known["sessions"]) if known else ident["sessions"]
    all_symbols = list(known["symbols"]) if known and known.get("symbols") else ident["symbols"]
    want_sessions = list(sessions) if sessions is not None else all_sessions
    want_symbols = list(symbols) if symbols is not None else all_symbols
    stray = [s for s in want_sessions if s not in all_sessions]
    if stray:
        raise HoldoutRefused(f"{dataset_dir}: sessions {stray} are not in this dataset")
    decisions = _scope_decisions(reg, want_sessions, want_symbols)
    if not decisions:
        raise HoldoutRefused(f"{dataset_dir}: no session / symbol selected; refusing")
    worst = max(decisions, key=lambda d: _SEVERITY.get(d["state"], 3))
    state = worst["state"]
    if state == "in_sample":
        return {"state": "in_sample"}
    if state == "unregistered":
        raise HoldoutRefused(
            f"{dataset_dir}: session {worst['session']} / {worst['symbol']} is not in "
            f"{REGISTRY_REL}; register the dataset before reading it (fails closed)"
        )
    if prereg_id is None:
        raise HoldoutRefused(
            f"{dataset_dir}: session {worst['session']} / {worst['symbol']} is {state.upper()} "
            "in the holdout registry; a read needs a signed prereg id"
        )
    for d in decisions:
        if d["state"] == "in_sample":
            continue
        r = d["rule"] or {}
        allowed = r.get("reserved_for" if d["state"] == "unread" else "spent_by", [])
        if prereg_id not in allowed:
            raise HoldoutRefused(
                f"{dataset_dir}: prereg {prereg_id!r} is not listed for {d['state']} scope "
                f"{d['session']}/{d['symbol']} (allowed: {list(allowed)})"
            )
    if not prereg_is_signed(prereg_id, root):
        raise HoldoutRefused(f"{dataset_dir}: prereg {prereg_id!r} is not a signed prereg")
    check_prereg_identity(prereg_id, ident["dataset_version"], root)
    return {"state": state, "authorised_by": prereg_id}


def check_prereg_identity(prereg_id: str, dataset_version: str | None, root: Path | None) -> None:
    """Review finding 1: a prereg document that binds an ``analysis_identity``
    authorises a restricted read only from the code, configs, lock and
    interpreter it bound, and only of a dataset version it bound.  This runs
    inside :func:`guard`, so an entry point that skips its own identity check
    still cannot read the holdout under that prereg.  Legacy preregs (no
    block; their looks are spent) and blackboard ids carry nothing to check."""
    r = Path(root or REPO)
    p = r / prereg_id
    if not (prereg_id.endswith(".json") and p.is_file()):
        return
    bound = json.loads(p.read_text(encoding="utf-8")).get("analysis_identity")
    if not bound:
        return
    from iap.agents.analysis_identity import IdentityError, environment_mismatches

    try:
        bad = environment_mismatches(r, bound)
    except IdentityError as exc:
        bad = [str(exc)]
    if dataset_version not in (bound.get("data_versions") or []):
        bad.append(f"dataset_version {dataset_version} is not bound by the prereg")
    if bad:
        raise HoldoutRefused(
            f"prereg {prereg_id!r} does not authorise this read: " + "; ".join(bad)
        )


# --------------------------------------------------------------- seed / CLI --
#: (session date) -> classification used to seed; the symbols rule makes the
#: 2025/26 days "spent" for the three registered names and "unread" for the rest.
_R7_IDS = ["a6c5af759082b68f", "de2488a2a245cf2c"]  # EQ10/1s, EQ05/1s blackboard prereg ids
_BREADTH = "research/preregs/breadth_book_prereg.json"
_IN_SAMPLE = [
    "2019-01-30", "2019-03-27", "2019-07-30", "2019-08-30", "2019-10-18", "2019-10-30",
    "2019-12-30", "2020-01-30", "2021-07-13", "2021-08-13",
]  # fmt: skip
_R7_DAYS = [
    "2025-11-28", "2025-12-08", "2025-12-09", "2025-12-10", "2025-12-11", "2025-12-12",
    "2026-06-12",
]  # fmt: skip
_V18_DAYS = ["2026-05-15", "2026-05-18"]
#: v1.18 REL-04b: the B8 v2 headline replays its three evaluation days
_B8V2 = "research/preregs/b8v2_prereg.json"
_B8V2_DAYS = ["2025-12-10", "2026-05-15", "2026-05-18"]
_THREE = ["AAPL", "MSFT", "QQQ"]


def seed_sessions() -> dict[str, list[dict[str, Any]]]:
    s: dict[str, list[dict[str, Any]]] = {
        d: [{"symbols": "*", "state": "in_sample"}] for d in _IN_SAMPLE
    }
    unread = {"symbols": "*", "state": "unread", "reserved_for": [_BREADTH]}
    for d in _R7_DAYS:
        s[d] = [
            {
                "symbols": _THREE,
                "state": "spent",
                "spent_by": list(_R7_IDS)
                + ["research/r7/r7_confirm_prereg.json", "b8-oos-2026-10-16"]
                + ([_B8V2] if d in _B8V2_DAYS else []),
            },
            unread,
        ]
    for d in _V18_DAYS:
        s[d] = [
            {
                "symbols": _THREE,
                "state": "spent",
                "spent_by": ["v1.8.0-holdout", "auc01", "b8-oos-2026-10-16"]
                + ([_B8V2] if d in _B8V2_DAYS else []),
            },
            unread,
        ]
    return s


def seed(data_root: Path) -> dict[str, Any]:
    """Build the registry from the datasets on disk (``data_root`` = data/real)."""
    sessions = seed_sessions()
    datasets: dict[str, Any] = {}
    for man in sorted(Path(data_root).rglob("dataset.json")):
        d = man.parent
        if "research" in d.parts[len(Path(data_root).parts) :]:
            continue
        ident = dataset_identity(d, rehash=False)
        if ident["kind"] != "real" or not ident["dataset_version"]:
            continue
        unknown = [x for x in ident["sessions"] if x not in sessions]
        if unknown:
            raise SystemExit(f"{d}: sessions {unknown} have no classification; add them first")
        decisions = _scope_decisions({"sessions": sessions}, ident["sessions"], ident["symbols"])
        state = max((x["state"] for x in decisions), key=lambda z: _SEVERITY[z])
        datasets[ident["dataset_version"]] = {
            "label": d.name,
            "content_hash": ident["content_hash"],
            "sessions": ident["sessions"],
            "symbols": ident["symbols"],
            "state": state,
        }
    return seal(
        {
            "schema": SCHEMA,
            "x-version": 1,
            "description": __doc__.split("\n\n")[0],
            "datasets": datasets,
            "sessions": sessions,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="rebuild the registry from data/real (writes the file)")
    s.add_argument("--data-root", type=Path, default=REPO / "data" / "real")
    s.add_argument("--out", type=Path, default=REPO / REGISTRY_REL)
    sub.add_parser("check", help="verify the committed registry's body hash")
    c = sub.add_parser("guard", help="dry-run a read of a dataset directory")
    c.add_argument("dataset_dir", type=Path)
    c.add_argument("--prereg-id")
    a = ap.parse_args(argv)
    if a.cmd == "seed":
        a.out.write_text(render(seed(a.data_root)), encoding="utf-8")
        print(f"wrote {a.out}")
    elif a.cmd == "check":
        load_registry()
        print("registry body hash ok")
    else:
        try:
            print(json.dumps(guard(a.dataset_dir, purpose="cli", prereg_id=a.prereg_id), indent=2))
        except HoldoutRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
