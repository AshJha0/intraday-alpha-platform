"""F3 acceptance study: freeze-consistency check (UNSIGNED DRAFT; reads no data).

Cross-checks PROTOCOL.md, declaration.json, core.py, the runner and the bind step (what the prereg will bind):
same n, seeds, alpha_op, FWER target, K set, G set, regimes, chunk size, mandatory vs sensitivity cells, and that every
required file and parameter is bound in the prereg identity.

  python check_freeze.py [--report FREEZE_CONSISTENCY.md]      exit 1 on any failure
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "python" / "src"))

import core  # noqa: E402
import joint_bound  # noqa: E402
import run_f3_accept as fa  # noqa: E402

FORBIDDEN_SEED_RANGES = ((20281100, 20281199), (20271001, 20271004))
REQUIRED_BOUND = {
    "research/v118/f3_accept/run_f3_accept.py",
    "research/v118/f3_accept/core.py",
    "research/v118/f3_accept/joint_bound.py",
    "research/v118/f3_accept/check_freeze.py",
    "research/v118/f3_accept/declaration.json",
}
REQUIRED_INPUTS = {
    "research/v118/F3_SPEC_DRAFT.md",
    "docs/release/REVIEWER_F3_SPEC_DECISION_2026-10-24.md",
    "docs/release/REVIEWER_F3_FREEZE_DECISION_2026-10-24.md",
    "research/v118/f3_accept/JOINT_BOUND.md",
    "research/v118/f3_spec/generators.py",
    "research/v118/f3_spec/procedures.py",
}
PROTOCOL_PHRASES = (
    "no market data",
    "sentinel only",
    "does not\nautomatically close F3",
    "A failed study is final",
    "never resets or reseeds",
    "nine sensitivity results",
)


def protocol_params(text: str) -> dict:
    m = re.search(r"```json freeze-params\n(.*?)\n```", text, re.S)
    if not m:
        raise ValueError("PROTOCOL.md has no freeze-params block")
    return json.loads(m.group(1))


def run_checks() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        out.append((name, bool(ok), detail))

    decl = json.loads((HERE / "declaration.json").read_text(encoding="utf-8"))
    proto = (HERE / "PROTOCOL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    pp = protocol_params(proto)
    km = core.k_max(int(decl["n_per_cell"]), float(decl["fwer_target"]), float(decl["ucb_conf"]))
    want = {
        "alpha_op": decl["alpha_op"],
        "fwer_target": decl["fwer_target"],
        "ucb_conf": decl["ucb_conf"],
        "n_per_cell": decl["n_per_cell"],
        "sensitivity_n_per_cell": decl["sensitivity"]["n_per_cell"],
        "chunk": decl["chunk"],
        "families": decl["families"],
        "mandatory_regimes": decl["mandatory"]["regimes"],
        "sensitivity_regimes": decl["sensitivity"]["regimes"],
        "G": decl["mandatory"]["G"],
        "seed_base": decl["seeds"]["base"],
        "dry_run_seed_base": decl["seeds"]["dry_run_base"],
        "rho_a1": decl["generators"]["rho_a1"],
        "scale_ratio": decl["generators"]["scale_ratio"],
        "k_max": km,
        "mandatory_evaluations": len(decl["mandatory"]["regimes"])
        * len(decl["mandatory"]["G"])
        * len(decl["families"]),
        "sensitivity_evaluations": len(decl["sensitivity"]["regimes"])
        * len(decl["sensitivity"]["G"])
        * len(decl["families"]),
    }
    diff = {k: (pp.get(k), v) for k, v in want.items() if pp.get(k) != v}
    chk(
        "PROTOCOL.md freeze-params block == declaration.json (and derived k_max / evaluation counts)",
        not diff,
        str(diff),
    )
    chk("sensitivity.G == mandatory.G", decl["sensitivity"]["G"] == decl["mandatory"]["G"])
    chk("sensitivity excluded from the verdict", decl["sensitivity"]["in_verdict"] is False)
    chk(
        "chunk size is 10,000 and a divisor of n",
        decl["chunk"] == 10000 and decl["n_per_cell"] % decl["chunk"] == 0,
    )
    chk("alpha_op 0.020, FWER target 0.025, n 50,000, K {3,6,16}, G {5,7,10}",
        (decl["alpha_op"], decl["fwer_target"], decl["n_per_cell"], decl["families"], decl["mandatory"]["G"])
        == (0.02, 0.025, 50000, [3, 6, 16], [5, 7, 10]))  # fmt: skip
    cl = fa.cells(decl)
    chk("12 cells: 9 mandatory + 3 sensitivity (a1)", len(cl) == 12 and sum(m for *_, m in cl) == 9
        and {r for r, _, m in cl if not m} == {"a1"})  # fmt: skip
    seeds = [core.seed_for(decl, r, g) for r, g, _ in cl] + [
        core.seed_for(decl, r, g, True) for r, g, _ in cl
    ]
    chk("24 distinct seeds, none inside a forbidden range", len(set(seeds)) == 24
        and not any(a <= s <= b for s in seeds for a, b in FORBIDDEN_SEED_RANGES))  # fmt: skip
    chk("JOINT_BOUND.md == joint_bound.py output",
        (HERE / "JOINT_BOUND.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        == joint_bound.render(joint_bound.table(decl)))  # fmt: skip
    for name in ("core.py", "run_f3_accept.py", "joint_bound.py"):
        src = (HERE / name).read_text(encoding="utf-8")
        lits = [x for x in ("50000", "10000", "20291100", "20291900") if x in src]
        chk(f"{name} hard-codes no frozen parameter literal", not lits, str(lits))
    chk(
        "core.run_cell takes the chunk size from the declaration",
        'decl["chunk"]' in (HERE / "core.py").read_text(encoding="utf-8"),
    )
    args = fa.identity_args(decl)
    chk(
        "bind: all required scripts bound",
        REQUIRED_BOUND <= set(args["scripts"]),
        str(REQUIRED_BOUND - set(args["scripts"])),
    )
    chk(
        "bind: all required inputs bound",
        REQUIRED_INPUTS <= set(args["inputs"]),
        str(REQUIRED_INPUTS - set(args["inputs"])),
    )
    missing = [
        p for p in [*args["scripts"], *args["inputs"], decl["protocol"]] if not (ROOT / p).is_file()
    ]
    chk("bind: every bound path exists in the repo", not missing, str(missing))
    chk(
        "bind: thresholds == the whole declaration (every parameter is bound)",
        args["thresholds"] == decl,
    )
    chk("bind: data_versions is the sentinel and the identity says no market data",
        args["data_versions"] == ["synthetic-no-data"] and "NO market data" in args["family"]
        and decl["market_data_consumed"] is False)  # fmt: skip
    chk(
        "bind: prereg protocol path == PROTOCOL.md",
        decl["protocol"] == "research/v118/f3_accept/PROTOCOL.md",
    )
    missing_phr = [p for p in PROTOCOL_PHRASES if p not in proto]
    chk(
        "PROTOCOL.md states resume, finality, sentinel, no-data and no-closure rules",
        not missing_phr,
        str(missing_phr),
    )
    return out


def render(res: list[tuple[str, bool, str]]) -> str:
    lines = [
        "# F3 acceptance study: freeze-consistency report (generated by check_freeze.py; UNSIGNED DRAFT)",
        "",
        "| check | result |",
        "|---|---|",
    ]
    for name, ok, detail in res:
        lines.append(f"| {name} | {'OK' if ok else 'FAIL ' + detail} |")
    lines += ["", f"{sum(ok for _, ok, _ in res)}/{len(res)} checks OK."]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    res = run_checks()
    text = render(res)
    if "--report" in argv:
        Path(argv[argv.index("--report") + 1]).write_text(text, encoding="utf-8", newline="\n")
    sys.stdout.write(text)
    return 0 if all(ok for _, ok, _ in res) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
