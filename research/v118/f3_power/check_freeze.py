"""F3 power diagnostic: freeze-consistency check (UNSIGNED DRAFT; reads no data).

Cross-checks PROTOCOL.md, the specification, declaration.json, the code and the bind step (what the prereg will bind):
same n, chunk, seeds, grid, alpha_op, K set, G set, regimes, MDE / bootstrap / ratio / integrity constants, and that every
required file and parameter is bound in the prereg identity.

  python check_freeze.py [--report FREEZE_CONSISTENCY.md]      exit 1 on any failure
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "python" / "src"))

import power_core as pc  # noqa: E402
import power_math as pm  # noqa: E402
import run_f3_power as fp  # noqa: E402

FORBIDDEN_SEED_RANGES = (
    (20281100, 20281199),
    (20291100, 20291199),
    (20291900, 20291999),
    (20271001, 20271004),
)
REQUIRED_BOUND = {
    "research/v118/f3_power/run_f3_power.py",
    "research/v118/f3_power/power_core.py",
    "research/v118/f3_power/power_math.py",
    "research/v118/f3_power/check_freeze.py",
    "research/v118/f3_power/declaration.json",
    "research/v118/f3_accept/core.py",
}
REQUIRED_INPUTS = {
    "research/v118/F3_POWER_DIAGNOSTIC_SPEC.md",
    "docs/release/REVIEWER_F3_SPEC_DECISION_2026-10-24.md",
    "docs/release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md",
    "docs/release/REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md",
    "research/v118/f3_accept/DECISION.json",
}
PROTOCOL_PHRASES = (
    "NON-GATING",
    "no market data",
    "sentinel only",
    "A completed run is final",
    "never resets or reseeds",
    "VOID (programming integrity)",
    "mu = d * sigma_RMS / sqrt(G)",
    "noncentrality-scale effect",
    "MDE80 > 16",
    "evaluated first",
)
SPEC_PHRASES = (
    "mu = d * sigma_RMS / sqrt(G)",
    "d in {0.5, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16}",
    "10,000",
    "VOID_INTEGRITY",
    "20301105",
    "frozen mapping",
    "first** index",
)
ACC_PREREG = "research/preregs/f3_accept_prereg.json"
ACC_CORE_REL = "research/v118/f3_accept/core.py"
PACKAGE_FILES = (
    "PROTOCOL.md",
    "declaration.json",
    "power_core.py",
    "power_math.py",
    "run_f3_power.py",
    "check_freeze.py",
)
# a shift expression built from the effect and the RMS scale must always be divided by sqrt(G)
_SQ = r"(?:math\.)?sqrt\(\s*g\s*\)"
UNDIVIDED_MU = re.compile(rf"d\s*\*?\s*sigma_rms(?!\s*/\s*{_SQ})(?!\([^)]*\)\s*/\s*{_SQ})", re.I)
LITERALS = ("20301100", "20301900", "20301500", "10000", "5000", "0.025", "1000")


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
    spec = (
        (ROOT / "research/v118/F3_POWER_DIAGNOSTIC_SPEC.md")
        .read_text(encoding="utf-8")
        .replace("\r\n", "\n")
    )
    pp = protocol_params(proto)
    want = {
        "alpha_op": decl["alpha_op"],
        "n_per_cell": decl["n_per_cell"],
        "chunk": decl["chunk"],
        "families": decl["families"],
        "regimes": decl["regimes"],
        "G": decl["G"],
        "grid": decl["grid"],
        "planted_index": decl["planted_index"],
        "power_target": decl["power_target"],
        "seed_base": decl["seeds"]["base"],
        "dry_run_seed_base": decl["seeds"]["dry_run_base"],
        "bootstrap_seed_base": decl["seeds"]["bootstrap_base"],
        "scale_ratio": decl["generators"]["scale_ratio"],
        "bootstrap_B": decl["bootstrap"]["B"],
        "ratio_min_count": decl["ratio"]["min_count"],
        "integrity_tau": decl["integrity"]["tau"],
        "integrity_tau_dry": decl["integrity"]["tau_dry"],
        "stream_cells": len(decl["regimes"]) * len(decl["G"]),
        "power_estimates": len(decl["regimes"])
        * len(decl["G"])
        * len(decl["families"])
        * len(decl["grid"]),
    }
    diff = {k: (pp.get(k), v) for k, v in want.items() if pp.get(k) != v}
    chk(
        "PROTOCOL.md freeze-params block == declaration.json (and derived counts)",
        not diff,
        str(diff),
    )
    chk("grid strictly increasing, positive, last value 16, 12 values",
        decl["grid"] == sorted(set(decl["grid"])) and decl["grid"][0] > 0 and decl["grid"][-1] == 16 and len(decl["grid"]) == 12)  # fmt: skip
    chk(
        "n, chunk: n = 10,000 and the chunk divides n",
        decl["n_per_cell"] == 10000 and decl["n_per_cell"] % decl["chunk"] == 0,
    )
    chk("alpha_op 0.020, K {3,6,16}, G {5,7,10}, regimes a0 b0 b1, planted index 0 present in every family",
        (decl["alpha_op"], decl["families"], decl["G"], decl["regimes"]) == (0.02, [3, 6, 16], [5, 7, 10], ["a0", "b0", "b1"])
        and decl["planted_index"] == 0)  # fmt: skip
    chk(
        "9 stream cells, 324 power estimates",
        len(fp.cells(decl)) == 9 and want["power_estimates"] == 324,
    )
    seeds = {
        "stream": [pc.seed_for(decl, r, g) for r, g in fp.cells(decl)],
        "dry": [pc.seed_for(decl, r, g, True) for r, g in fp.cells(decl)],
        "bootstrap": [pc.bootstrap_seed(decl, r, g) for r, g in fp.cells(decl)],
    }
    allseeds = [s for v in seeds.values() for s in v]
    chk("27 distinct seeds (9 stream, 9 dry-run, 9 bootstrap), none in a forbidden range",
        len(set(allseeds)) == 27 and not any(a <= s <= b for s in allseeds for a, b in FORBIDDEN_SEED_RANGES))  # fmt: skip
    chk("stream seeds are 20301105 ... 20301130 as written in the specification",
        sorted(seeds["stream"]) == [20301105, 20301107, 20301110, 20301115, 20301117, 20301120, 20301125, 20301127, 20301130])  # fmt: skip
    for name in ("power_core.py", "power_math.py", "run_f3_power.py"):
        src = (HERE / name).read_text(encoding="utf-8")
        lits = [x for x in LITERALS if re.search(rf"(?<![\w.]){re.escape(x)}(?![\w.])", src)]
        chk(f"{name} hard-codes no frozen parameter literal", not lits, str(lits))
    chk("core.run_cell takes chunk, grid, alpha_op and planted index from the declaration",
        all(s in (HERE / "power_core.py").read_text(encoding="utf-8") for s in ('decl["chunk"]', 'decl["grid"]', 'decl["alpha_op"]', 'decl["planted_index"]')))  # fmt: skip
    args = fp.identity_args(decl)
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
    chk("bind: data_versions is the sentinel; identity says NO market data and NON-GATING; declaration says none consumed",
        args["data_versions"] == ["synthetic-no-data"] and "NO market data" in args["family"]
        and "NON-GATING" in args["family"] and decl["market_data_consumed"] is False)  # fmt: skip
    chk(
        "bind: prereg protocol path == PROTOCOL.md",
        decl["protocol"] == "research/v118/f3_power/PROTOCOL.md",
    )
    miss_p = [p for p in PROTOCOL_PHRASES if p not in proto]
    chk(
        "PROTOCOL.md states non-gating, no-data, sentinel, finality, stream, void and effect rules",
        not miss_p,
        str(miss_p),
    )
    miss_s = [p for p in SPEC_PHRASES if p not in spec]
    chk(
        "specification carries the same grid, n, seeds, effect formula, CRN mapping and void rule",
        not miss_s,
        str(miss_s),
    )
    chk("integrity band sanity: at d = 0 the reference equals alpha_op / K",
        all(abs(pm.pi_ref(decl["alpha_op"], g, k, 0.0) - decl["alpha_op"] / k) < 1e-12 for g in decl["G"] for k in decl["families"]))  # fmt: skip
    chk("analytic reference reaches 80 % at d = 16 for every a0 (G, K): the grid can contain every homogeneous MDE80",
        all(pm.pi_ref(decl["alpha_op"], g, k, 16.0) >= decl["power_target"] for g in decl["G"] for k in decl["families"]))  # fmt: skip
    chk(
        "effect formula in the declaration is mu = d * sigma_RMS / sqrt(G)",
        "mu = d * sigma_RMS / sqrt(G)" in decl["effect"]["formula"],
    )
    # --- documentation check 1: one definition of mu everywhere; excess fully specified and bound
    undivided = {
        n: UNDIVIDED_MU.findall((HERE / n).read_text(encoding="utf-8")) for n in PACKAGE_FILES
    }
    chk(
        "mu = d * sigma_RMS / sqrt(G) is the only effect expression in the declaration, protocol, code and this check",
        not any(undivided.values()),
        str({n: v for n, v in undivided.items() if v}),
    )
    chk(
        "the shift code computes d * sigma_rms / sqrt(g)",
        "d * sigma_rms(decl, regime, g) / math.sqrt(g)"
        in (HERE / "power_core.py").read_text(encoding="utf-8"),
    )
    ex = decl["integrity"]["excess"]
    declared = {int(k): v for k, v in ex["value_by_K"].items()}
    chk(
        "integrity.excess is specified in the declaration (formula, comparison, why) for exactly the K set",
        sorted(declared) == sorted(decl["families"])
        and all(ex.get(k) for k in ("formula", "comparison", "why")),
    )
    chk(
        "declared excess values equal the analytic 1 - (1 - alpha_op/K)^(K-1) to 1e-12",
        all(abs(declared[k] - pm.excess(decl["alpha_op"], k)) < 1e-12 for k in declared),
    )
    chk(
        "the declaration states that tau is a heuristic tolerance, not a simultaneous guarantee",
        "heuristic tolerance" in decl["integrity"]["tau_note"]
        and "NOT a simultaneous" in decl["integrity"]["tau_note"],
    )
    chk(
        "no document says tau is five times the standard error without the heuristic qualification",
        all(
            "five times" not in t or "heuristic tolerance" in t
            for t in (proto, spec, json.dumps(decl))
        ),
    )
    # --- documentation check 2: the imported acceptance core is bound by its exact fingerprint
    core_text = (ROOT / ACC_CORE_REL).read_bytes().decode("utf-8")
    core_text = core_text.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
    core_sha = hashlib.sha256(core_text.encode("utf-8")).hexdigest()
    chk(
        "bind: the imported acceptance core.py is in the bound script list (identity records its LF-normalised sha256)",
        ACC_CORE_REL in args["scripts"]
        and ACC_CORE_REL == str(pc.ACC_CORE.relative_to(ROOT).as_posix()),
    )
    acc_doc = json.loads((ROOT / ACC_PREREG).read_text(encoding="utf-8"))
    chk(
        f"the acceptance core.py hash {core_sha[:16]}... equals the hash bound by the signed acceptance prereg",
        acc_doc["analysis_identity"]["scripts"].get(ACC_CORE_REL) == core_sha,
    )
    return out


def render(res: list[tuple[str, bool, str]]) -> str:
    lines = [
        "# F3 power diagnostic: freeze-consistency report (generated by check_freeze.py; UNSIGNED DRAFT)",
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
