"""Fill a confirmation-profile prereg template (v2.0 round 5) into an UNSIGNED prereg.

A template under ``research/v2/prereg_templates/`` fixes everything the profile
fixes (statistic, procedure, HAC lag, alpha, sample-size cap, refusal
conditions, registry state) and leaves named slots (``{"$fill": name}``) for
what a registration chooses (family, horizon, T_min, economic screen, data,
the registered run).  This tool:

1. substitutes every slot from a fill file (one value per slot name; a name
   used twice, such as ``partition``, takes one value);
2. refuses an unfilled slot, an unknown fill key, a deferred profile (PF-3),
   and a registration that the profile gate would refuse
   (``iap.validation.profiles``: horizon class, family, procedure with an
   operating domain, T_min at or above the validated T, economic screen,
   registry state), checked against the committed evidence before anything
   is written;
3. builds the identity-v2 block (:func:`iap.agents.analysis_identity.build_identity`)
   over the bound scripts and inputs at HEAD, with ``data_versions`` = the
   dataset id and the image digest the registered run names;
4. records the registration's protocol document (``protocol``,
   ``protocol_sha256``) and, when given, the planted dry-run report as the
   ``dry_run`` block, the two things ``prereg_bind_identity.py sign`` checks
   besides the identity (sign refuses a prereg without a dry run);
5. writes the prereg with ``signed: false``.  Signing is the owner's step
   (``tools/prereg_bind_identity.py sign``), never this tool's.

    python tools/prereg_from_template.py fill \\
        --template research/v2/prereg_templates/PF-1.template.json \\
        --fill research/v2/registrations/<name>.fill.json \\
        --protocol research/v2/families/<family>.md \\
        [--planted-dry-run <report.json>] --out research/preregs/<name>.json

Refusals exit 2 with nothing written; the tree must be clean apart from the
output file, and every bound script and input must equal its bytes at HEAD
(the checks of ``prereg_bind_identity.py bind``).  Reads no data.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "tools"), str(ROOT / "python" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from iap.agents.analysis_identity import IdentityError, build_identity  # noqa: E402
from iap.agents.planted_dry_run import DryRunRefused, build_dry_run_block  # noqa: E402
from iap.validation import profiles  # noqa: E402

PARTITIONS = ("b", "c")  # Track B test, Track C confirmation (U0 section 2)


class TemplateRefused(ValueError):
    """The template or fill would not make a registration the profile gate accepts."""


def _norm_sha(data: bytes) -> str:
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slot_names(node: Any) -> set[str]:
    """Every slot name left in a template (or a partly filled document)."""
    if isinstance(node, dict):
        if "$fill" in node:
            return {str(node["$fill"])}
        return set().union(*(slot_names(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(slot_names(v) for v in node)) if node else set()
    return set()


def _substitute(node: Any, fill: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$fill" in node:
            return copy.deepcopy(fill[node["$fill"]])
        return {k: _substitute(v, fill) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, fill) for v in node]
    return node


def fill_template(
    template: dict[str, Any], fill: dict[str, Any], *, evidence_root: Path | None = None
) -> dict[str, Any]:
    """The filled ``registration``, ``registered_run`` and ``identity`` sections.
    Raises :class:`TemplateRefused`."""
    if template.get("kind") != "prereg_template":
        raise TemplateRefused("not a prereg template")
    pid = str(template.get("template_id"))
    prof = profiles.PROFILES.get(pid)
    if template.get("deferred") or prof is None or prof.deferred:
        raise TemplateRefused(
            f"profile not validated ({pid} deferred); no registration can be filled"
        )
    wanted = slot_names(template)
    unknown = sorted(set(fill) - wanted)
    missing = sorted(wanted - set(fill))
    if unknown or missing:
        raise TemplateRefused(
            f"fill keys differ from the template slots: missing {missing}, unknown {unknown}"
        )
    out = {
        k: _substitute(template[k], fill) for k in ("registration", "registered_run", "identity")
    }
    reg, run = out["registration"], out["registered_run"]
    if run["dataset_files"] is None:
        del run["dataset_files"]
    if run["partition"] not in PARTITIONS:
        raise TemplateRefused(
            f"partition {run['partition']!r} is not 'b' (Track B) or 'c' (Track C)"
        )
    if reg["data"]["origin"] not in ("synthetic", "real"):
        raise TemplateRefused("data origin must be 'synthetic' or 'real'")
    mu = reg["m_min"]["min_units_per_session"]
    if not isinstance(mu, int) or isinstance(mu, bool) or mu < 1:
        raise TemplateRefused("m_min.min_units_per_session must be a positive integer")
    if not (
        isinstance(reg["economic_screen"]["margin"], (int, float))
        and reg["economic_screen"]["margin"] >= 0
    ):
        raise TemplateRefused("economic_screen.margin must be a non-negative number")
    if (
        not isinstance(run["command"], list)
        or not run["command"]
        or not all(isinstance(x, str) for x in run["command"])
    ):
        raise TemplateRefused("registered_run.command must be a non-empty list of strings")
    dom = profiles.operating_domain(pid, evidence_root)
    if dom is None:
        raise TemplateRefused(f"profile not validated (no committed evidence for {pid})")
    reason = profiles._registration_checks(prof, dom, reg)
    if reason:
        raise TemplateRefused(f"the profile gate would refuse this registration: {reason}")
    return out


def build_prereg(
    root: Path,
    template_rel: str,
    fill: dict[str, Any],
    out_rel: str,
    *,
    protocol_rel: str,
    dry_run_rel: str | None = None,
    commit: str | None = None,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """The unsigned prereg document (nothing written)."""
    root = Path(root)
    raw = (root / template_rel).read_bytes()
    template = json.loads(raw.decode("utf-8"))
    sections = fill_template(template, fill, evidence_root=evidence_root)
    reg, run, ident = sections["registration"], sections["registered_run"], sections["identity"]
    try:
        block = build_identity(
            root,
            scripts=list(ident["scripts"]),
            inputs=list(ident["inputs"] or []),
            data_versions=[run["dataset_hash_id"]],
            family=f"{reg['profile']}:{','.join(map(str, reg['family']))}",
            metrics=[reg["statistic"]],
            thresholds={
                "alpha_op": reg["alpha_op"],
                "hurdle_bps": reg["economic_screen"]["hurdle_bps"],
                "margin": reg["economic_screen"]["margin"],
                "T_min": reg["sample_size"]["T_min"],
            },
            commit=commit,
            image_digest=run["image_digest"],
        )
    except IdentityError as exc:
        raise TemplateRefused(f"analysis identity: {exc}") from exc
    doc = {
        "x-version": 1,
        "kind": "analysis_prereg",
        "prereg_id": out_rel,
        "protocol": protocol_rel,
        "protocol_sha256": _norm_sha((root / protocol_rel).read_bytes()),
        "template": {
            "path": template_rel,
            "sha256": _norm_sha(raw),
            "revision": template.get("template_revision"),
        },
        "status": "DRAFT: filled from a profile template, analysis identity bound, NOT signed",
        "signed": False,
        "bound_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "registration": reg,
        "registered_run": run,
        "analysis_identity": block,
    }
    if dry_run_rel:
        try:
            doc["dry_run"] = build_dry_run_block(root, dry_run_rel)
        except DryRunRefused as exc:
            raise TemplateRefused(str(exc)) from exc
    else:
        doc["status"] += "; NO planted dry run bound (sign will refuse)"
    return doc


def main(argv: list[str] | None = None) -> int:
    from prereg_bind_identity import Refused, _rel, check_committed, dirty_paths

    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fill", help="write the unsigned prereg filled from a template")
    f.add_argument("--template", required=True, type=Path)
    f.add_argument("--fill", required=True, type=Path)
    f.add_argument(
        "--protocol", required=True, type=Path, help="the registration's protocol document"
    )
    f.add_argument("--planted-dry-run", type=Path, help="committed planted dry-run report to bind")
    f.add_argument("--out", required=True, type=Path)
    f.add_argument("--dry-run", action="store_true", help="print the document, write nothing")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    out_rel = _rel(root, a.out)
    if (root / out_rel).is_file():
        old = json.loads((root / out_rel).read_text(encoding="utf-8"))
        if old.get("signed") is True or "signature" in old:
            raise Refused(f"{out_rel} is already signed; re-filling would void the signature")
    dirty = dirty_paths(root, {out_rel})
    if dirty:
        raise Refused(f"working tree is dirty: {dirty[:10]}")
    fill = json.loads(a.fill.read_text(encoding="utf-8"))
    tmpl_rel = _rel(root, a.template)
    proto_rel = _rel(root, a.protocol)
    dry_rel = _rel(root, a.planted_dry_run) if a.planted_dry_run else None
    ident = fill.get("scripts", []) + (fill.get("inputs") or [])
    check_committed(root, [tmpl_rel, proto_rel, *ident, *([dry_rel] if dry_rel else [])])
    try:
        doc = build_prereg(
            root, tmpl_rel, fill, out_rel, protocol_rel=proto_rel, dry_run_rel=dry_rel
        )
    except TemplateRefused as exc:
        raise Refused(str(exc)) from exc
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if a.dry_run:
        print(text, end="")
        return 0
    (root / out_rel).parent.mkdir(parents=True, exist_ok=True)
    with (root / out_rel).open("w", encoding="ascii", newline="\n") as fh:
        fh.write(text)
    print(f"wrote {out_rel} from {tmpl_rel} ({doc['registration']['profile']}); NOT signed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
