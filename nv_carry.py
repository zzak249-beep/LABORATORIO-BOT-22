"""
IDEA 4 · CARRY CUBIERTO — comprar spot y abrir corto del mismo tamaño en el perpetuo.

El precio da igual (lo que gana una pata lo pierde la otra); se cobra el funding mientras sea positivo.
Es el negocio de Ethena y de las mesas de basis, pero hecho de forma selectiva:
  · solo las monedas con funding alto y estable (media de 3 días ≥ CR_ENTRY_APR anual)
  · no entrar si la prima del perpetuo sobre el spot está en el pico (CR_MAX_BASIS)
  · salir cuando el funding se apaga (≤ CR_EXIT_APR)
Costes reales: 4 operaciones (spot 0.10 % + perp 0.05 % por lado, más deslizamiento) y rehacer margen si sube mucho.
Riesgos que el papel NO elimina: corto liquidado si la moneda se dispara y no hay margen, despegue spot/perp en pánico.
"""
import logging
import time

import config as C
from bingx import BingXError
from nv_common import DAY, fmt_row, summarize, z_bonf  # noqa: F401
from universe import pretty

log = logging.getLogger("carry")


def apr(events, t_end, days):
    """Funding anualizado (%) de los eventos de los últimos `days` días antes de t_end."""
    ev = [r for t, r in events if t_end - days * DAY < t <= t_end]
    if not ev:
        return None
    return sum(ev) / days * 365 * 100


def cost_in_out(n):
    return n * (C.CR_SPOT_FEE + C.COST_PCT) / 100        # una pata spot + una perp, por cada entrada o salida


# ── simulación diaria (investigación) ──
def simulate(data, dates, entry_apr, exit_apr, slots, universe):
    cap = C.CR_CAPITAL
    eq = cap
    held = {}               # sym → {"u": unidades, "s": spot, "p": perp, "p0": perp entrada, "n": nocional}
    curve, stats = [], {"fund": 0.0, "fees": 0.0, "basis": 0.0, "entries": 0, "rehedge": 0, "days_pos": 0}
    syms = universe
    for d in dates:
        # 1) valorar lo abierto: funding cobrado y cambio de la prima (spot − perp)
        for s, h in list(held.items()):
            px = data[s]["px"].get(d)
            if not px:
                continue
            S, P = px
            fund = sum(r for t, r in data[s]["fund"] if d - DAY < t <= d) * h["u"] * P
            basis = h["u"] * (S - h["s"]) - h["u"] * (P - h["p"])
            eq += fund + basis
            stats["fund"] += fund
            stats["basis"] += basis
            h["s"], h["p"] = S, P
            if P / h["p0"] - 1 > C.CR_REHEDGE / 100:          # rehacer margen del corto (vender spot, reponer)
                c = h["n"] * (C.CR_SPOT_FEE + C.COST_PCT) / 100
                eq -= c
                stats["fees"] += c
                stats["rehedge"] += 1
                h["p0"] = P
        # 2) salidas
        for s in list(held):
            a = apr(data[s]["fund"], d, C.CR_LOOKBACK_D)
            if a is None or a < exit_apr:
                c = cost_in_out(held[s]["n"])
                eq -= c
                stats["fees"] += c
                del held[s]
        # 3) entradas
        free = slots - len(held)
        if free > 0:
            cands = []
            for s in syms:
                if s in held:
                    continue
                px = data[s]["px"].get(d)
                if not px:
                    continue
                a = apr(data[s]["fund"], d, C.CR_LOOKBACK_D)
                if a is None or a < entry_apr:
                    continue
                if (px[1] / px[0] - 1) * 100 > C.CR_MAX_BASIS:
                    continue
                cands.append((a, s, px))
            cands.sort(reverse=True)
            for a, s, (S, P) in cands[:free]:
                n = eq / slots / (1 + 1 / C.CR_PERP_LEV)
                c = cost_in_out(n)
                eq -= c
                stats["fees"] += c
                stats["entries"] += 1
                held[s] = {"u": n / P, "s": S, "p": P, "p0": P, "n": n}
        stats["days_pos"] += len(held)
        curve.append((d, eq))
    return curve, stats


def curve_stats(curve):
    import math
    eq = [e for _, e in curve]
    r = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    if len(r) < 20:
        return None
    m = sum(r) / len(r)
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    yrs = len(r) / 365
    cut = int(len(r) * 0.7)
    is_ = sum(r[:cut]) / cut * 365 * 100 if cut else 0
    oos = sum(r[cut:]) / max(1, len(r) - cut) * 365 * 100
    return {"apr": (eq[-1] / eq[0] - 1) / yrs * 100 if yrs else 0, "sharpe": m / sd * math.sqrt(365) if sd else 0,
            "t": m / sd * math.sqrt(len(r)) if sd else 0, "mdd": mdd * 100, "is": is_, "oos": oos}


def load(bx, tg, n_syms):
    contracts = bx.load_contracts()
    spot = bx.spot_symbols()
    vol = {}
    for t in bx.tickers():
        try:
            vol[t["symbol"]] = float(t.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            pass
    syms = [s for s, c in contracts.items() if c["cls"] == "crypto" and s in spot and vol.get(s, 0) >= C.MIN_QUOTE_VOL]
    syms = sorted(syms, key=lambda s: -vol[s])[:n_syms]
    tg.send(f"🔬 CARRY · {len(syms)} monedas con spot y perpetuo · {C.RESEARCH_DAYS} días de velas diarias + funding…")
    data = {}
    for s in syms:
        try:
            pk = bx.klines_history(s, "1d", C.RESEARCH_DAYS, DAY)
            sk = bx.spot_klines_history(s, "1d", C.RESEARCH_DAYS)
            if len(pk) < 60 or len(sk) < 60:
                continue
            fund = bx.funding_history(s, pk[0][0])
        except BingXError as e:
            log.warning("%s: %s", s, e)
            continue
        sp = {r[0] + DAY: r[4] for r in sk}
        px = {r[0] + DAY: (sp[r[0] + DAY], r[4]) for r in pk if r[0] + DAY in sp}
        data[s] = {"px": px, "fund": sorted(fund)}
    return data, syms


def research(bx, tg):
    t0 = time.time()
    data, syms = load(bx, tg, max(C.CR_UNIVERSE, 30))
    if not data:
        return "🔬 <b>IDEA 4 · CARRY</b>: sin datos (¿la API spot de BingX respondió?)"
    dates = sorted({d for v in data.values() for d in v["px"]})
    ranked = [s for s in syms if s in data]
    variants = [(e, x, u) for e in (10, 20, 40) for x in (0, 5) for u in (15, len(ranked))]
    thr = z_bonf(len(variants))
    lines = [f"🔬 <b>IDEA 4 · CARRY CUBIERTO</b> (spot largo + perp corto) · {len(data)} monedas · {len(dates)} días "
             f"({time.time() - t0:.0f}s)",
             f"capital {C.CR_CAPITAL:,.0f} · {C.CR_SLOTS} posiciones · perp a {C.CR_PERP_LEV:g}x · costes spot "
             f"{C.CR_SPOT_FEE}% + perp {C.COST_PCT}% por lado",
             f"(rent. anual · Sharpe · t · maxDD · rent. 70%|30%) · ✅ = t ≥ {thr:.2f} (Bonferroni {len(variants)})", ""]
    best = None
    for e, x, u in variants:
        curve, st = simulate(data, dates, e, x, C.CR_SLOTS, ranked[:u])
        m = curve_stats(curve)
        name = f"entra≥{e:>2}% sale<{x}% top{u}"
        if not m:
            lines.append(f"<code>{name}</code> sin datos")
            continue
        star = " ✅" if m["t"] >= thr else (" ·" if m["t"] >= 2 else "")
        yrs = len(curve) / 365
        lines.append(f"<code>{name}</code> {m['apr']:+.1f}%/año · Sh {m['sharpe']:.2f} · t {m['t']:+.2f}{star} · "
                     f"DD {m['mdd']:.1f}% · {m['is']:+.1f}|{m['oos']:+.1f}")
        lines.append(f"      funding {st['fund'] / C.CR_CAPITAL / yrs * 100:+.1f}%/año · costes "
                     f"{-st['fees'] / C.CR_CAPITAL / yrs * 100:.1f} · prima {st['basis'] / C.CR_CAPITAL / yrs * 100:+.1f} · "
                     f"{st['entries']} entradas · ocupación {st['days_pos'] / len(curve) / C.CR_SLOTS * 100:.0f}%")
        if best is None or m["sharpe"] > best[1]["sharpe"]:
            best = (name, m)
    # referencia: carry permanente en BTC+ETH (lo que paga el mercado "de base")
    base = [s for s in ("BTC-USDT", "ETH-USDT") if s in data]
    if base:
        curve, st = simulate(data, dates, -1000, -1000, len(base), base)
        m = curve_stats(curve)
        if m:
            lines += ["", f"Referencia BTC+ETH siempre: {m['apr']:+.1f}%/año · Sh {m['sharpe']:.2f} · DD {m['mdd']:.1f}%"]
    if best:
        lines += [f"Mejor: {best[0]} · {best[1]['apr']:+.1f}%/año · Sharpe {best[1]['sharpe']:.2f}"]
    lines.append("OJO: la curva del carry es muy suave → t y Sharpe salen inflados. Juzga por el 30% final, la "
                 "rentabilidad neta frente a BTC+ETH y la caída máxima, no por el ✅.")
    lines.append("Sin riesgo de precio en teoría; los riesgos son el corto liquidado en un pump sin margen y el "
                 "despegue spot/perp. El funding cambia de régimen: un año alcista paga mucho más que uno bajista.")
    return "\n".join(lines)


# ── en vivo (papel) ──
class CarryBook:
    """Cartera de carry en papel. Se revisa tras cada cobro de funding (cada hora basta)."""

    def __init__(self, st, tg):
        self.st = st.setdefault("carry", {"eq": C.CR_CAPITAL, "held": {}, "closed": [], "last": 0,
                                          "fund": 0.0, "fees": 0.0, "basis": 0.0})
        self.tg = tg

    def step(self, bx, now, contracts, perp_px, perp_vol):
        b = self.st
        try:
            spot_t = bx.spot_tickers()
        except BingXError as e:
            log.warning("spot tickers: %s", e)
            return
        # valorar
        for s, h in list(b["held"].items()):
            S, P = spot_t.get(s, (None,))[0], perp_px.get(s)
            if not S or not P:
                continue
            try:
                ev = bx.funding_history(s, h["t"] + 1)
            except BingXError:
                ev = []
            fund = sum(r for t, r in ev if t > h["t"]) * h["u"] * P
            if ev:
                h["t"] = max(t for t, _ in ev)
            basis = h["u"] * (S - h["s"]) - h["u"] * (P - h["p"])
            b["eq"] += fund + basis
            b["fund"] += fund
            b["basis"] += basis
            h["got"] += fund
            h["s"], h["p"] = S, P
        if now - b["last"] < 3600 * 1000:
            return
        b["last"] = now
        # salidas y entradas con el funding de los últimos días
        cands = []
        univ = [s for s, c in contracts.items() if c["cls"] == "crypto" and s in spot_t
                and perp_vol.get(s, 0) >= C.MIN_QUOTE_VOL]
        univ = sorted(univ, key=lambda s: -perp_vol[s])[:C.CR_UNIVERSE]
        for s in set(univ) | set(b["held"]):
            try:
                ev = bx.funding_history(s, now - C.CR_LOOKBACK_D * DAY - 1)
            except BingXError:
                continue
            a = apr(ev, now, C.CR_LOOKBACK_D)
            if s in b["held"]:
                if a is None or a < C.CR_EXIT_APR:
                    self._exit(s, a)
            elif a is not None and a >= C.CR_ENTRY_APR:
                S, P = spot_t[s][0], perp_px.get(s)
                if P and (P / S - 1) * 100 <= C.CR_MAX_BASIS:
                    cands.append((a, s, S, P))
        cands.sort(reverse=True)
        for a, s, S, P in cands[:C.CR_SLOTS - len(b["held"])]:
            n = b["eq"] / C.CR_SLOTS / (1 + 1 / C.CR_PERP_LEV)
            c = cost_in_out(n)
            b["eq"] -= c
            b["fees"] += c
            b["held"][s] = {"u": n / P, "s": S, "p": P, "n": n, "t": now, "t0": now, "apr": a, "got": 0.0}
            self.tg.send(f"💰 <b>CARRY ENTRA</b> · {pretty(s)} · funding {a:.0f}%/año (media {C.CR_LOOKBACK_D} d)\n"
                         f"   compra spot {S:.6g} + corto perp {P:.6g} · nocional {n:,.0f} USDT · coste {c:.2f}\n"
                         f"   sale si el funding baja de {C.CR_EXIT_APR:g}%/año · (papel)")

    def _exit(self, s, a):
        b = self.st
        h = b["held"].pop(s)
        c = cost_in_out(h["n"])
        b["eq"] -= c
        b["fees"] += c
        days = (time.time() * 1000 - h["t0"]) / DAY
        b["closed"].append({"s": s, "days": round(days, 2), "fund": round(h["got"], 2), "n": round(h["n"], 2)})
        self.tg.send(f"💰 <b>CARRY SALE</b> · {pretty(s)} · funding ahora {a if a is not None else 0:.0f}%/año\n"
                     f"   {days:.1f} días · funding cobrado {h['got']:+.2f} USDT · coste salida {c:.2f}")

    def text(self):
        b = self.st
        r = (b["eq"] / C.CR_CAPITAL - 1) * 100
        held = ", ".join(f"{pretty(s)} {h['apr']:.0f}%" for s, h in b["held"].items()) or "nada"
        return (f"CARRY: equity {b['eq']:,.2f} ({r:+.2f}%) · funding {b['fund']:+.2f} · costes {-b['fees']:.2f} · "
                f"prima {b['basis']:+.2f} · {len(b['closed'])} cerradas · abiertas: {held}")
