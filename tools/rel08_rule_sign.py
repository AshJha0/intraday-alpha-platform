"""Sign the REL-08 continuation rule's JSON copy (v1.18 REL-08, programme 9.5).

    python tools/rel08_rule_sign.py <repo_root> [--keyfile PATH] [--maintainer ashjha0] [--dry-run]

Reads ``research/v118/rel08_rule_DRAFT.json`` (checked against the markdown
rule), builds the REL-02a ``analysis_identity`` at HEAD (the screen scripts,
the ``python/src/iap`` tree hash, the commit, the rule's dataset version,
family ``rel08_screen``, the metrics and the thresholds), signs with the
trust-root maintainer key and writes ``research/v118/rel08_rule_signed.json``,
then loads it back through ``load_signed_rule``.

Run it on a CLEAN checkout of the commit the screen will run at (after the
REL-08 PR is merged), before any variant is run or any result is examined.
It reads no market data.  Since v1.19 R4 the draft must carry a ``dry_run``
block (:mod:`iap.agents.planted_dry_run`) whose report demonstrates every
outcome of the rule; without it the tool refuses.  Commit the signed file afterwards: the identity
accepts HEAD or any descendant whose ``python/src/iap`` tree is unchanged.
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from iap.agents.analysis_identity import build_identity  # noqa: E402
from iap.agents.planted_dry_run import DryRunRefused, require_dry_run  # noqa: E402
from iap.research.rel08_rule import (  # noqa: E402
    BOUND_SCRIPTS,
    FAMILY,
    METRICS,
    SIGNED_REL,
    load_draft,
    load_signed_rule,
    render,
    sign_rule,
)

KEYFILE = Path("C:/Users/ashis/.iap_keys/maintainer.key")
MAINTAINER = "ashjha0"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path)
    ap.add_argument("--keyfile", type=Path, default=KEYFILE)
    ap.add_argument("--maintainer", default=MAINTAINER)
    ap.add_argument(
        "--signed-by",
        default="Claude under the owner's written delegation (REL08_CONTINUATION_RULE.md header)",
    )
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    dirty = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--",
            "python/src",
            "tools",
            "research/v118",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit(f"refusing: uncommitted changes under the bound paths:\n{dirty}")
    draft = load_draft(root)
    # v1.19 R4 (defect D-09): no signature without a planted dry run that
    # demonstrates every outcome the rule can return.  The v1.18 REL-08
    # signature, whose C1 made ``continue`` and ``stop`` unreachable, would
    # have been refused here.
    try:
        dry = require_dry_run(root, draft.doc)
    except DryRunRefused as exc:
        raise SystemExit(f"refusing: planted dry run (R4): {exc}") from exc
    print(f"planted dry run {dry['path']}: verdict values {dry['verdict_values']} demonstrated")
    identity = build_identity(
        root,
        scripts=list(BOUND_SCRIPTS),
        data_versions=[str(draft.doc["dataset"]["dataset_version"])],
        family=FAMILY,
        metrics=list(METRICS),
        thresholds=dict(draft.thresholds),
    )
    print(f"identity at commit {identity['commit']} tree {identity['code_tree_sha256'][:12]}")
    if a.dry_run:
        print(f"[dry-run] would sign {draft.path} -> {SIGNED_REL}")
        return 0
    import json

    from iap.agents import signing, trust

    key = json.loads(a.keyfile.read_text(encoding="ascii"))[a.maintainer]
    pub = (trust.read_roots(root) or {}).get("maintainers", {}).get(a.maintainer)
    if signing.public_of(key) != pub:
        raise SystemExit("maintainer key does not match research/agents/trust_roots.json")
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    doc = sign_rule(draft.doc, key, a.maintainer, identity, a.signed_by, now)
    out = root / SIGNED_REL
    with out.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(render(doc))
    rule = load_signed_rule(out, root)
    print(f"wrote {SIGNED_REL}: signed, message sha256 {rule.message_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
