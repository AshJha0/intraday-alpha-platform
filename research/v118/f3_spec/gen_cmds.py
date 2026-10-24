"""Write the three detached worker .cmd files (CRLF) for the pilot."""

from pathlib import Path

D = Path(__file__).resolve().parent
PY = r"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe"
WORK = {
    "w1": [("a0", 7, 6000), ("b0", 7, 6000), ("c0", 7, 3000)],
    "w2": [("a1", 7, 6000), ("b1", 7, 6000), ("c1", 7, 3000)],
    "w3": [(r, g, 6000) for g in (5, 10) for r in ("a0", "b0", "b1", "a1")]
    + [(r, g, 3000) for g in (5, 10) for r in ("c0", "c1")],
}
for w, cells in WORK.items():
    lines = [
        "@echo off",
        "set PYTHONUTF8=1",
        f"set PYTHONPATH={D.parents[2]}\\python\\src;{D}",
        f"cd /d {D}",
    ]
    for r, g, n in cells:
        lines.append(f'"{PY}" run_cell.py {r} {g} {n} out\\{r}_G{g}.json >> logs\\{w}.log 2>&1')
    (D / f"{w}.cmd").write_bytes(("\r\n".join(lines) + "\r\n").encode())
print("ok")
