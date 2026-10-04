"""
IDEA 3 · WEEKEND GAP — perpetuos TradFi que cotizan 24/7 (oro, cobre, índices, acciones "724" de BingX).

Por qué podría funcionar: el fin de semana el mercado real (CME, bolsas) está CERRADO; el precio del perpetuo
lo mueve solo flujo cripto fino y apalancado. Cuando el mercado real reabre, el precio de referencia vuelve a
mandar y los excesos del finde se corrigen. Casi nadie lo opera porque estos perpetuos son nuevos y poco vistos.
Regla: movimiento viernes 21:00 → domingo 21:00 UTC mayor que WK_Z desviaciones típicas → operar en contra,
entrando el domingo 21:00 (antes de abrir CME) o el lunes 14:00 (con la bolsa de EE. UU. abriendo).
"""
import logging
import math
import time
from datetime import datetime, timezone

import config as C
from bingx import BingXError
from nv_common import DAY, HOUR, fmt_row, funding_between, summarize, z_bonf

log = logging.getLogger("weekend")


def weekend_symbols(contracts):
    return [s for s, c in contracts.items() if c["cls"] != "crypto" and "724" in s and s not in C.BLACKLIST]


def _dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def sigma_daily(closes_by_t, t_end, days=20):
    """Vol diaria a partir de rendimientos horarios de días laborables antes de t_end."""
    r = []
    t = t_end - days * 7 // 5 * DAY
    prev = None
    while t <= t_end:
        c = closes_by_t.get(t)
        if c and prev and _dt(t).weekday() < 5:
            r.append(math.log(c / prev))
        prev = c if c else prev
        t += HOUR
    if len(r) < 50:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(24)


def signal_at(closes, fri21, entry_t, z_thr):
    """Devuelve (dir, z, sigma, m) o None. closes: {cierre_ms: precio}."""
    pf, ps = closes.get(fri21), closes.get(fri21 + 2 * DAY)          # viernes 21:00 y domingo 21:00
    if not pf or not ps or not closes.get(entry_t):
        return None
    sd = sigma_daily(closes, fri21)
    if not sd:
        return None
    m = math.log(ps / pf)
    z = m / (sd * math.sqrt(2))
    if abs(z) < z_thr:
        return None
    return (-1 if m > 0 else 1), z, sd, m


def simulate(bars_by_close, closes, fund, fri21, entry_kind, z_thr, hold_h, cost):
    entry_t = fri21 + 2 * DAY if entry_kind == "dom21" else fri21 + 2 * DAY + 17 * HOUR   # lunes 14:00
    sig = signal_at(closes, fri21, entry_t, z_thr)
    if not sig:
        return None
    d, z, sd, m = sig
    entry = closes[entry_t]
    stop = entry * (1 - d * C.WK_STOP_SIG * sd)
    exit_px, tx, why = None, None, "tiempo"
    t = entry_t + HOUR
    while t <= entry_t + hold_h * HOUR:
        b = bars_by_close.get(t)
        if b:
            o, h, l, c = b
            if d > 0 and l <= stop:
                exit_px, tx, why = min(o, stop), t, "stop"
                break
            if d < 0 and h >= stop:
                exit_px, tx, why = max(o, stop), t, "stop"
                break
            exit_px, tx = c, t
        t += HOUR
    if exit_px is None or (why == "tiempo" and tx < entry_t + hold_h * HOUR - 2 * HOUR):
        return None
    ret = d * (exit_px / entry - 1) - 2 * cost - d * funding_between(fund, entry_t, tx)
    return {"t": fri21, "ret": ret, "why": why, "z": z}


def research(bx, tg):
    t0 = time.time()
    contracts = bx.load_contracts()
    syms = weekend_symbols(contracts)
    tg.send(f"🔬 WEEKEND · {len(syms)} perpetuos TradFi 24/7 · velas 1h de {C.RESEARCH_DAYS} días…")
    data = {}
    for s in syms:
        try:
            k = bx.klines_history(s, "1h", C.RESEARCH_DAYS * 24, HOUR)
            if len(k) < 24 * 30:
                continue
            fund = bx.funding_history(s, k[0][0])
        except BingXError:
            continue
        if not any(_dt(b[0]).weekday() == 5 for b in k):
            continue                                     # no cotiza el sábado → no es 24/7
        data[s] = ({b[0] + HOUR: (b[1], b[2], b[3], b[4]) for b in k}, {b[0] + HOUR: b[4] for b in k}, fund, k[0][0])
    cost = C.COST_PCT / 100
    # todos los viernes 21:00 UTC del periodo
    now = int(time.time() * 1000)
    start = now - C.RESEARCH_DAYS * DAY
    d0 = _dt(start).replace(hour=21, minute=0, second=0, microsecond=0)
    fridays = []
    t = int(d0.timestamp() * 1000)
    while t < now - 4 * DAY:
        if _dt(t).weekday() == 4:
            fridays.append(t)
        t += DAY
    variants = [(z, e, h) for z in (0.5, 1.0) for e in ("dom21", "lun14") for h in (24, 48)]
    thr = z_bonf(len(variants))
    lines = [f"🔬 <b>IDEA 3 · WEEKEND GAP</b> · {len(data)} TradFi 24/7 · {len(fridays)} findes ({time.time() - t0:.0f}s)",
             "contra el movimiento vie 21:00 → dom 21:00 UTC si supera z desviaciones",
             f"agrupado por fin de semana · ✅ = |t| ≥ {thr:.2f} (Bonferroni {len(variants)})",
             "símbolos: " + ", ".join(sorted(s.split("-")[0][4:].replace("724", "") for s in data))[:300], ""]
    best = None
    for z, e, h in variants:
        res = []
        for bars, closes, fund, _ in data.values():
            for f in fridays:
                r = simulate(bars, closes, fund, f, e, z, h, cost)
                if r:
                    res.append(r)
        s = summarize(res, lambda x: x["t"])
        name = f"z≥{z:.1f} {e} {h}h"
        lines.append(fmt_row(name, s, thr))
        if s.get("n") and (best is None or s["t"] > best[1]["t"]):
            best = (name, s)
    if best:
        lines += ["", f"Mejor: {best[0]} · t {best[1]['t']:+.2f} · {best[1]['mean'] * 100:+.2f}% por operación"]
    if not data:
        lines.append("BingX no devolvió perpetuos 24/7 con historia suficiente.")
    return "\n".join(lines)


# ── en vivo ──
def scan(bx, st, now, contracts):
    plans = []
    dt = _dt(now)
    if C.WK_ENTRY == "dom21":
        ok = dt.weekday() == 6 and dt.hour == 21 and dt.minute < 30
        fri21 = int(dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000) - 2 * DAY
    else:
        ok = dt.weekday() == 0 and dt.hour == 14 and dt.minute < 30
        fri21 = int(dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000) - 2 * DAY - 17 * HOUR
    week = str(fri21)
    if not ok or st.get("wk_done") == week:
        return plans
    st["wk_done"] = week
    entry_t = int(dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    for s in weekend_symbols(contracts):
        try:
            k = bx.klines(s, "1h", limit=24 * 35)
        except BingXError:
            continue
        closes = {b[0] + HOUR: b[4] for b in k if b[0] + HOUR <= entry_t}
        sig = signal_at(closes, fri21, entry_t, C.WK_Z)
        if not sig:
            continue
        d, z, sd, m = sig
        plans.append({"module": "weekend", "symbol": s, "side": d, "stop_pct": C.WK_STOP_SIG * sd * 100,
                      "close_at": entry_t + C.WK_HOLD_H * HOUR,
                      "why": f"finde {m * 100:+.2f}% ({z:+.1f}σ) con el mercado real cerrado → corrección"})
    return plans
