"""Opening/closing cross prints + Nasdaq matched volume for a few symbols."""

import json
import sys
import time

from iap.marketdata import itch50 as I

path, out = sys.argv[1], sys.argv[2]
syms = sys.argv[3].split(",")
r = I.Itch50Reader(
    path, symbols=syms, limit_messages=int(sys.argv[4]) if len(sys.argv) > 4 else None
)
loc2sym = {}
st = {
    s: {
        "cross": {},
        "exec_E": 0,
        "exec_C_printable": 0,
        "exec_C_nonprintable": 0,
        "trade_P": 0,
        "cross_Q": 0,
        "regular_session_volume": 0,
    }
    for s in syms
}
t0 = time.time()
for m in r:
    if isinstance(m, I.StockDirectory):
        loc2sym[m.locate] = m.stock.strip()
        continue
    sym = loc2sym.get(getattr(m, "locate", -1))
    if sym not in st:
        continue
    d = st[sym]
    reg = 34_200_000_000_000 <= m.ts <= 57_600_000_000_000
    if isinstance(m, I.OrderExecutedWithPrice):
        d["exec_C_printable" if m.printable == "Y" else "exec_C_nonprintable"] += m.shares
        if reg and m.printable == "Y":
            d["regular_session_volume"] += m.shares
    elif isinstance(m, I.OrderExecuted):
        d["exec_E"] += m.shares
        if reg:
            d["regular_session_volume"] += m.shares
    elif isinstance(m, I.Trade):
        d["trade_P"] += m.shares
        if reg:
            d["regular_session_volume"] += m.shares
    elif isinstance(m, I.CrossTrade):
        d["cross_Q"] += m.shares
        if reg:
            d["regular_session_volume"] += m.shares
        d["cross"].setdefault(m.cross_type, []).append(
            {"price_e4": m.price, "shares": m.shares, "ts": m.ts}
        )
for d in st.values():
    d["nasdaq_matched_volume"] = d["exec_E"] + d["exec_C_printable"] + d["trade_P"] + d["cross_Q"]
json.dump(
    {
        "file": path,
        "symbols": st,
        "messages_read": r.messages_read,
        "elapsed_s": round(time.time() - t0, 1),
    },
    open(out, "w"),
    indent=2,
)
print("done", time.time() - t0)
