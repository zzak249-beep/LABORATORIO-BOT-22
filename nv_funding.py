"""
IDEA 2 · FUNDING CLOCK — operar el reloj del funding.

Por qué podría funcionar: con un funding extremo (p. ej. +0.1 % cada 8 h) quien está en el lado que paga
tiene un incentivo enorme a cerrar JUSTO antes del cobro y volver a entrar después. Ese flujo es predecible
en el tiempo (la hora del cobro se sabe de antemano), no depende de ningún patrón de velas.
  pre  = entrar W minutos antes del cobro CONTRA el lado que paga y mantener hasta pasado el cobro
         (además se COBRA el funding: el lado contrario lo recibe)
  post = entrar justo tras el cobro A FAVOR del lado que pagaba (vuelven a entrar → rebote)
"""
import logging
import time

import config as C
from bingx import BingXError
from nv_common import fmt_row, summarize, z_bonf

log = logging.getLogger("funding")
M15 = 15 * 60_000


def _trade(bars, T, rate, W, mode, cost, stop_pct):
    """bars: {open_ms: (o,h,l,c)} de 15m. Devuelve dict de la operación o None si faltan velas."""
    sgn = 1 if rate > 0 else -1
    if mode == "pre":
        t_in, t_last, d = T - W * 60_000, T, -sgn        # sale al cierre de la vela del cobro (T+15m)
    else:
        t_in, t_last, d = T, T + W * 60_000 - M15, sgn
    if t_in not in bars or t_last not in bars:
        return None
    entry = bars[t_in][0]
    stop = entry * (1 - d * stop_pct / 100)
    exit_px, why = None, "tiempo"
    t = t_in
    while t <= t_last:
        b = bars.get(t)
        if b is None:
            return None
        o, h, l, c = b
        if d > 0 and l <= stop:
            exit_px, why = min(o, stop), "stop"
            break
        if d < 0 and h >= stop:
            exit_px, why = max(o, stop), "stop"
            break
        exit_px = c
        t += M15
    ret = d * (exit_px / entry - 1) - 2 * cost
    if mode == "pre" and why == "tiempo":
        ret += abs(rate)                                  # estábamos en el lado que cobra
    return {"t": T, "ret": ret, "why": why}


def research(bx, tg):
    t0 = time.time()
    now = int(time.time() * 1000)
    contracts = bx.load_contracts()
    vol = {}
    for t in bx.tickers():
        try:
            vol[t["symbol"]] = float(t.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            pass
    syms = [s for s, c in contracts.items() if c["cls"] == "crypto" and vol.get(s, 0) >= C.MIN_QUOTE_VOL]
    syms = sorted(syms, key=lambda s: -vol[s])[:C.RESEARCH_SYMBOLS]
    tg.send(f"🔬 FUNDING CLOCK · {len(syms)} monedas · velas de 15m (~95 días, lo que da BingX) + funding…")
    data = {}
    for i, s in enumerate(syms):
        try:
            k = bx.klines_history(s, "15m", 96 * 100, M15)
            if len(k) < 500:
                continue
            fund = bx.funding_history(s, k[0][0])
        except BingXError:
            continue
        data[s] = ({b[0]: (b[1], b[2], b[3], b[4]) for b in k}, fund)
        if (i + 1) % 20 == 0:
            log.info("funding: %d/%d", i + 1, len(syms))
    n_ev = sum(len(f) for _, f in data.values())
    cost = C.COST_PCT / 100
    variants = [(thr, W, m) for thr in (0.03, 0.08) for W in (30, 60, 120) for m in ("pre", "post")]
    z = z_bonf(len(variants))
    days = (now - min((min(b) for b, _ in data.values() if b), default=now)) / 86_400_000
    lines = [f"🔬 <b>IDEA 2 · FUNDING CLOCK</b> · {len(data)} monedas · {n_ev} cobros · {days:.0f} días ({time.time() - t0:.0f}s)",
             "pre = contra el lado que paga desde W min antes hasta pasado el cobro (+cobra funding) · post = rebote tras el cobro",
             f"agrupado por hora de cobro · ✅ = |t| ≥ {z:.2f} (Bonferroni {len(variants)})", ""]
    best = None
    for thr, W, mode in variants:
        res = []
        for bars, fund in data.values():
            for T, rate in fund:
                T = (T + 60_000) // M15 * M15            # la hora de cobro puede traer milisegundos de más
                if abs(rate) * 100 >= thr:
                    r = _trade(bars, T, rate, W, mode, cost, C.FC_STOP_PCT)
                    if r:
                        res.append(r)
        s = summarize(res, lambda x: x["t"])
        name = f"|f|≥{thr:.2f}% {W:>3}m {mode:4s}"
        lines.append(fmt_row(name, s, z))
        if s.get("n") and (best is None or s["t"] > best[1]["t"]):
            best = (name, s)
    if best:
        lines += ["", f"Mejor: {best[0]} · t {best[1]['t']:+.2f} · {best[1]['mean'] * 100:+.3f}% por operación"]
    lines.append(f"Coste incluido: {C.COST_PCT * 2:.2f}% ida y vuelta. Solo ~95 días de 15m en BingX: muestra corta.")
    return "\n".join(lines)


# ── en vivo ──
def scan(bx, st, now, vol):
    """Planes en la ventana previa al cobro (pre) o justo después (post)."""
    plans = []
    try:
        prem = bx.premium_all()
    except BingXError as e:
        log.warning("premiumIndex: %s", e)
        return plans
    seen = st.setdefault("fc_seen", {})
    pend = st.setdefault("fc_pend", {})
    W = C.FC_WIN_MIN * 60_000
    for s, (rate, nxt, mark) in prem.items():
        if not nxt or vol.get(s, 0) < C.MIN_QUOTE_VOL or s in C.BLACKLIST:
            continue
        key = f"{s}:{nxt}"
        if abs(rate) * 100 < C.FC_THR:
            continue
        if C.FC_MODE == "pre" and nxt - W <= now < nxt - W + 5 * 60_000 and key not in seen:
            seen[key] = 1
            d = -1 if rate > 0 else 1
            plans.append({"module": "funding", "symbol": s, "side": d, "stop_pct": C.FC_STOP_PCT,
                          "close_at": nxt + 5 * 60_000,
                          "why": f"funding {rate * 100:+.3f}% cobra en {(nxt - now) / 60000:.0f} min · "
                                 f"los {'largos' if rate > 0 else 'cortos'} pagan y cierran"})
        if C.FC_MODE == "post" and nxt - 10 * 60_000 <= now < nxt:
            pend[s] = (nxt, rate)
    if C.FC_MODE == "post":
        for s, (T, rate) in list(pend.items()):
            if T <= now < T + 3 * 60_000 and f"{s}:{T}" not in seen:
                seen[f"{s}:{T}"] = 1
                plans.append({"module": "funding", "symbol": s, "side": 1 if rate > 0 else -1,
                              "stop_pct": C.FC_STOP_PCT, "close_at": T + W,
                              "why": f"tras el cobro de {rate * 100:+.3f}%: rebote esperado"})
            if now >= T + 3 * 60_000:
                del pend[s]
    if len(seen) > 5000:
        st["fc_seen"] = dict(list(seen.items())[-2000:])
    return plans
