"""Re-render the REL-08 scorecard from the committed screen JSON (v2.0 stage 3 P1).

    python tools/repro/render_rel08_scorecard.py \\
        --screen research/v118/rel08/screen_ds_20190130.json --out <dir>

Writes ``<out>/SCORECARD_<tag>.md`` through the screen runner's own renderer
(:func:`iap.research.rel08_screen.render_scorecard`), exactly as the runner
wrote the committed ``research/v118/rel08/SCORECARD_ds_20190130.md`` after its
run.  The screen JSON is the public result file; the market data behind it
(a feature store built with the Rust engine, F7 guide section 4.4) is not in
the repository, so this is the scorecard's re-derivation step, the counterpart
of B8 v2 ``analyse``.  Reads no market data, writes nothing else.  The tag is
the JSON's ``dataset.tag``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# appended, not inserted: a PYTHONPATH set by iap-repro --code-at (the renderer
# at the commit that wrote the committed scorecard) must win over this checkout
sys.path.append(str(ROOT / "python" / "src"))


def render(screen: Path, out: Path) -> Path:
    from iap.research.rel08_screen import render_scorecard

    raw = json.loads(screen.read_text(encoding="utf-8"))
    tag = (raw.get("dataset") or {}).get("tag") or screen.stem.removeprefix("screen_")
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"SCORECARD_{tag}.md"
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_scorecard(raw))
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--screen", type=Path, required=True, help="screen_<tag>.json")
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    a = ap.parse_args(argv)
    target = render(a.screen, a.out)
    print(f"wrote {target.name} to {a.out.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
