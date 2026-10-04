"""
IDEA 1 · LISTING DECAY — corto en monedas recién listadas.

Por qué podría funcionar (estructural, no un patrón de velas):
  · los primeros días de un perpetuo nuevo los venden airdrops, fondos con tokens desbloqueados y market makers;
  · casi no hay compradores naturales y el "hype" del listado ya está en el precio.
Regla (fijada antes de mirar resultados): corto a las LST_DELAY_H horas del listado, se mantiene LST_HOLD_D días,
stop sobre el máximo desde el listado. Opcional: largo de BTC del mismo nocional para quitar el efecto mercado.
Sesgo a tener en cuenta: las monedas deslistadas no salen en la API → faltan justo las que más cayeron
(el resultado medido es CONSERVADOR para el corto).
"""
import logging
import time

import config as C
from bingx import BingXError
from nv_common import DAY, HOUR, fmt_row, funding_between, summarize, z_bonf

log = logging.getLogger("listing")


def listing_hour(bx, sym, max_days=60):
    """Hora de listado (ms) si el símbolo es más joven que max_days; None si es antiguo o no hay datos."""
    d = bx.klines(sym, "1d", limit=min(1440, max_days + 2))
    if not d or len(d) >= max_days + 2:
        return None
    h = bx.klines(sym, "1h", limit=1000, start_time=d[0][0], end_time=d[0][0] + 1000 * HOUR - 1)
    return h[0][0] if h else d[0][0]


def simulate(bars, L0, fund, btc_close, delay_h, hold_d, stop_mode, cost):
    """Una operación. bars 1h desde el listado. Devuelve dict o None si no se puede evaluar."""
    t_entry = L0 + delay_h * HOUR
    idx = next((i for i, b in enumerate(bars) if b[0] + HOUR >= t_entry), None)
    if idx is None:
        return None
    entry = bars[idx][4]
    te = bars[idx][0] + HOUR
    peak = max(b[2] for b in bars[:idx + 1])
    stop = None
    if stop_mode == "pico":
        stop = peak * (1 + C.LST_STOP_BUF / 100)
        if stop / entry - 1 > C.LST_STOP_MAX / 100:
            return {"skip": "pico lejos"}
        stop = max(stop, entry * 1.02)
    elif stop_mode == "pct":
        stop = entry * (1 + C.LST_STOP_PCT / 100)
    tx_plan = te + int(hold_d * DAY)
    exit_px, tx, why = None, None, "tiempo"
    for b in bars[idx + 1:]:
        if b[0] + HOUR > tx_plan:
            break
        if stop and b[2] >= stop:
            exit_px, tx, why = max(b[1], stop), b[0] + HOUR, "stop"
            break
        exit_px, tx = b[4], b[0] + HOUR
    if exit_px is None or (why == "tiempo" and tx < tx_plan):
        return None                                  # aún no ha pasado el tiempo completo
    ret = (entry - exit_px) / entry - 2 * cost + funding_between(fund, te, tx)   # corto cobra funding positivo
    out = {"t": te, "ret": ret, "why": why, "entry": entry, "exit": exit_px, "risk": (stop / entry - 1) if stop else None}
    b0, b1 = btc_close.get(te), btc_close.get(tx)
    if b0 and b1:
        out["ret_h"] = ret + (b1 / b0 - 1) - 2 * cost
    return out


def research(bx, tg):
    t0 = time.time()
    now = int(time.time() * 1000)
    contracts = bx.load_contracts()
    syms = [s for s, c in contracts.items() if c["cls"] == "crypto" and s not in C.BLACKLIST]
    tg.send(f"🔬 LISTING · buscando monedas listadas en los últimos {C.RESEARCH_DAYS} días entre {len(syms)} perpetuos…")
    data = {}
    for i, s in enumerate(syms):
        try:
            d = bx.klines(s, "1d", limit=min(1440, C.RESEARCH_DAYS + 5))
        except BingXError:
            continue
        if not d or len(d) >= C.RESEARCH_DAYS + 5:
            continue
        if d[0][0] < now - C.RESEARCH_DAYS * DAY or d[0][0] > now - 4 * DAY:
            continue
        try:
            h = bx.klines(s, "1h", limit=1000, start_time=d[0][0], end_time=d[0][0] + 1000 * HOUR - 1)
            if not h:
                continue
            fund = bx.funding_history(s, h[0][0], h[0][0] + 20 * DAY)
        except BingXError:
            continue
        data[s] = (h[0][0], h, fund)
        if (i + 1) % 100 == 0:
            log.info("listing: %d/%d revisados, %d listados recientes", i + 1, len(syms), len(data))
    btc = bx.klines_history("BTC-USDT", "1h", C.RESEARCH_DAYS * 24 + 600, HOUR)
    btc_close = {b[0] + HOUR: b[4] for b in btc}
    cost = C.COST_PCT / 100

    if C._s("LST_GRID", "full").lower() == "confirm":     # prueba de confirmación fijada de antemano
        variants = [(72, 7, "pct"), (72, 14, "pct")]
    else:
        variants = [(d, h, s) for d in (24, 72) for h in (3, 7, 14) for s in ("pico", "none")]
    thr = z_bonf(len(variants) * 2)
    lines = [f"🔬 <b>IDEA 1 · LISTING DECAY</b> · {len(data)} listados en {C.RESEARCH_DAYS} días ({time.time() - t0:.0f}s)",
             "corto a las X h del listado · mantener Y días · stop sobre el pico o sin stop",
             f"agrupado por semana · ✅ = |t| ≥ {thr:.2f} (Bonferroni {len(variants) * 2} pruebas)", ""]
    best = None
    for dly, hold, stp in variants:
        res = [simulate(bars, L0, fund, btc_close, dly, hold, stp, cost) for L0, bars, fund in data.values()]
        skipped = sum(1 for r in res if r and "skip" in r)
        res = [r for r in res if r and "ret" in r]
        s1 = summarize(res, lambda x: x["t"] // (7 * DAY))
        hed = [dict(r, ret=r["ret_h"]) for r in res if "ret_h" in r]
        s2 = summarize(hed, lambda x: x["t"] // (7 * DAY))
        name = f"{dly}h {hold:>2}d {stp if stp != 'pct' else f'+{C.LST_STOP_PCT:g}%':4s}"
        lines.append(fmt_row(name, s1, thr) + (f" · {skipped} sin entrar" if skipped else ""))
        lines.append(fmt_row(name + " +BTC", s2, thr))
        for s in (s1, s2):
            if s.get("n") and (best is None or s["t"] > best[1]["t"]):
                best = (name, s)
    if best:
        lines += ["", f"Mejor: {best[0]} · t {best[1]['t']:+.2f} · media {best[1]['mean'] * 100:+.2f}% por operación"]
    lines.append("+BTC = cubierto con largo de BTC (mide el efecto listado sin el mercado). "
                 "Faltan las monedas ya deslistadas → resultado conservador para el corto.")
    return "\n".join(lines)


# ── en vivo ──
def scan(bx, st, now, contracts):
    """Detecta listados nuevos y devuelve planes de corto cuando toca."""
    known = st.setdefault("lst", {})               # sym → L0 ms | 0 (antiguo)
    done = st.setdefault("lst_done", [])
    plans = []
    for s, c in contracts.items():
        if c["cls"] != "crypto" or s in C.BLACKLIST or not c.get("api_open", True):
            continue
        if s not in known:
            try:
                L0 = listing_hour(bx, s, max_days=30)
            except BingXError:
                continue
            known[s] = L0 or 0
        L0 = known[s]
        if not L0 or s in done:
            continue
        t_entry = L0 + C.LST_DELAY_H * HOUR
        if not (t_entry <= now < t_entry + 3 * HOUR):
            if now >= t_entry + 3 * HOUR:
                done.append(s)                       # ventana pasada: no se persigue
            continue
        try:
            bars = bx.klines(s, "1h", limit=1000, start_time=L0, end_time=L0 + 1000 * HOUR - 1)
            px = bx.price(s)
        except BingXError:
            continue
        peak = max(b[2] for b in bars) if bars else px
        if C.LST_STOP == "pico":
            stop = max(peak * (1 + C.LST_STOP_BUF / 100), px * 1.02)
            if stop / px - 1 > C.LST_STOP_MAX / 100:
                done.append(s)
                continue
        elif C.LST_STOP == "pct":
            stop = px * (1 + C.LST_STOP_PCT / 100)
        else:
            stop = px * 1.9                        # sin stop "real": catástrofe muy lejos
        done.append(s)
        plans.append({"module": "listing", "symbol": s, "side": -1, "stop": stop,
                      "close_at": t_entry + int(C.LST_HOLD_D * DAY), "hedge": C.LST_HEDGE,
                      "why": f"listado hace {(now - L0) / HOUR:.0f} h · pico {peak:.6g} · {C.LST_HOLD_D:g} días"})
    st["lst_done"] = done[-2000:]
    return plans
