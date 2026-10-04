"""
Investigación honesta de TSMOM-C con datos de BingX (velas + funding reales).

Simula EXACTAMENTE la lógica del bot (signals.py + portfolio.py), día a día, con costes y funding,
y la compara con: 8 variantes (señal capada/signo × carry on/off × freno on/off), cartera
equiponderada solo-largos y BTC. Corrección de Bonferroni por probar 8 variantes.

Variables: RESEARCH_SYMBOLS (60), RESEARCH_DAYS (1095), RESEARCH_TF (1d).
Ojo: el universo se elige con el volumen de HOY → sesgo de supervivencia (favorece a los largos).
"""
import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

import config as C
from bingx import BingX, BingXError
from data import pick_universe
from notify import Telegram
from portfolio import Book, changes, stats, targets
from signals import AssetSignal
from universe import is_tradfi, pretty

log = logging.getLogger("backtest")

N_SYM = C._i("RESEARCH_SYMBOLS", 60)
DAYS = C._i("RESEARCH_DAYS", 1095)
TF = C._s("RESEARCH_TF", "1d").lower()
CACHE = os.path.join(C.DATA_DIR, "bt_cache")
BONF_T = 2.73     # |t| para p<0.05 bilateral con 8 pruebas


def day(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def load(bx, syms):
    os.makedirs(CACHE, exist_ok=True)
    tf_ms = C.tf_seconds(TF) * 1000
    bars = int(DAYS * C.bars_per_day(TF))
    out = {}
    for i, s in enumerate(syms):
        path = os.path.join(CACHE, f"{s}_{TF}_{DAYS}.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            with open(path) as f:
                out[s] = json.load(f)
            continue
        try:
            rows = bx.klines_history(s, TF, bars, tf_ms)
            now = int(time.time() * 1000)
            rows = [r for r in rows if r[0] + tf_ms <= now]
            fund = bx.funding_history(s, rows[0][0]) if rows else []
        except BingXError as e:
            log.warning("%s: %s", s, e)
            continue
        out[s] = {"rows": rows, "fund": fund}
        with open(path, "w") as f:
            json.dump(out[s], f)
        if (i + 1) % 10 == 0:
            log.info("descargados %d/%d", i + 1, len(syms))
    return out


def simulate(data, p, start_equity=10_000.0):
    tf_ms = C.tf_seconds(TF) * 1000
    bpd = C.bars_per_day(TF)
    sig = {s: AssetSignal(p, bpd, 260.0 if is_tradfi(s) else 365.0) for s in data}
    by_t = defaultdict(dict)
    for s, d in data.items():
        for r in d["rows"]:
            by_t[r[0]][s] = r[4]
    fund = {s: sorted(tuple(x) for x in d["fund"]) for s, d in data.items()}
    fi = {s: 0 for s in data}
    book = Book(start_equity, p.COST_PCT)
    curve, ew, npos, gross, n_trades = [], [], [], [], 0
    last_px = {}
    started = False
    for k, t in enumerate(sorted(by_t)):
        close_t = t + tf_ms
        prices, fsum, today = by_t[t], {}, {}
        for s, px in prices.items():
            ev = []
            f = fund[s]
            while fi[s] < len(f) and f[fi[s]][0] <= close_t:
                ev.append(f[fi[s]])
                fi[s] += 1
            g = sig[s].update(px, close_t, ev)
            g["days"] = g["bars"] / bpd
            today[s] = g
            fsum[s] = sum(x[1] for x in ev)
        # benchmark equiponderado (solo activos con historia suficiente)
        rets = [prices[s] / last_px[s] - 1 for s in prices if s in last_px and today[s]["days"] >= p.MIN_HISTORY_DAYS]
        last_px.update(prices)
        book.mark(prices, fsum)
        cur = book.weights()
        elig = {s: g for s, g in today.items() if g["days"] >= p.MIN_HISTORY_DAYS and g["sigma_ann"] > 0}
        held = {s: today[s] for s in cur if s in today}
        tgt = targets({**elig, **held}, cur, p, rebalance_day=k % max(1, p.REBAL_DAYS) == 0)
        ch = changes(cur, tgt)
        if ch:
            book.rebalance({s: b for s, _, _, b in ch}, prices)
            n_trades += sum(1 for c in ch if c[1] != "REDUCE" and c[1] != "AUMENTA")
        started = started or bool(book.units)
        if started:
            curve.append((t, book.equity))
            ew.append(sum(rets) / len(rets) if rets else 0.0)
            w = book.weights()
            npos.append(sum(1 for x in w.values() if abs(x) > 1e-12))
            gross.append(sum(abs(x) for x in w.values()))
    return {"curve": curve, "ew": ew, "npos": npos, "gross": gross, "book": book, "trades": n_trades}


def summarize(res, ppy):
    eq = [e for _, e in res["curve"]]
    if len(eq) < 30:
        return None
    a = stats(eq, ppy)
    cut = int(len(eq) * 0.7)
    a["is"] = stats(eq[:cut + 1], ppy)
    a["oos"] = stats(eq[cut:], ppy)
    years = defaultdict(list)
    for t, e in res["curve"]:
        years[day(t)[:4]].append(e)
    a["years"] = {y: v[-1] / v[0] - 1 for y, v in years.items() if len(v) > 5}
    yrs = a["n"] / ppy
    b = res["book"]
    a["turn"] = b.turnover / yrs if yrs else 0
    a["cost"] = b.cost_paid / eq[0] / yrs if yrs else 0
    a["fund"] = b.funding_paid / eq[0] / yrs if yrs else 0
    a["npos"] = sum(res["npos"]) / len(res["npos"])
    a["gross"] = sum(res["gross"]) / len(res["gross"])
    a["trades"] = res["trades"]
    a["start"], a["end"] = day(res["curve"][0][0]), day(res["curve"][-1][0])
    return a


def bench_from_returns(r, ppy):
    eq = [1.0]
    for x in r:
        eq.append(eq[-1] * (1 + x))
    return stats(eq, ppy)


def main():
    tg = Telegram(C.TG_TOKEN, C.TG_CHAT)
    bx = BingX(C.API_KEY, C.API_SECRET, C.VST)
    t0 = time.time()
    syms, _ = pick_universe(bx)
    syms = syms[:N_SYM]
    if "BTC-USDT" not in syms:
        syms.append("BTC-USDT")
    tg.send(f"🔬 Investigación TSMOM-C · {len(syms)} símbolos · {DAYS} días · {TF}\nDescargando velas y funding de BingX…")
    data = load(bx, syms)
    data = {s: d for s, d in data.items() if len(d["rows"]) > 30}
    ppy = 365 * C.bars_per_day(TF)
    base = SimpleNamespace(**{k: getattr(C, k) for k in dir(C) if k.isupper()})

    variants = []
    for sm in ("capada", "signo"):
        for carry in (True, False):
            for liq in (True, False):
                variants.append((f"{sm:6s} carry {'on ' if carry else 'off'} freno {'on ' if liq else 'off'}",
                                 dict(SIGNAL_MODE=sm, USE_CARRY=carry, USE_LIQ=liq, LIQ_FREEZE=liq and base.LIQ_FREEZE)))
    results = []
    for name, ov in variants:
        p = SimpleNamespace(**{**vars(base), **ov})
        r = simulate(data, p)
        a = summarize(r, ppy)
        results.append((name, a, r))
        log.info("%s → %s", name, "sin datos" if not a else f"Sharpe {a['sharpe']:.2f} t {a['t']:.2f}")

    ref = next((r for n, a, r in results if a), None)
    lines = [f"🔬 <b>TSMOM-C · resultados</b> ({len(data)} símbolos BingX, {TF}, {time.time() - t0:.0f}s)"]
    if not ref:
        tg.send("\n".join(lines + ["Sin datos suficientes."]))
        return
    a0 = summarize(ref, ppy)
    lines.append(f"Periodo {a0['start']} → {a0['end']} · coste {C.COST_PCT}%/lado · máx {C.MAX_POS} pos · vol obj {C.TARGET_VOL:.0f}%")
    lines.append("")
    lines.append("<b>Variantes</b> (Sharpe · t · CAGR · maxDD · Sharpe 70%|30%)")
    best = None
    for name, a, _ in results:
        if not a:
            lines.append(f"<code>{name}</code> sin datos")
            continue
        star = " ✅" if a["t"] >= BONF_T else (" ·" if a["t"] >= 2 else "")
        lines.append(f"<code>{name}</code> {a['sharpe']:+.2f} · t {a['t']:+.2f}{star} · {a['cagr'] * 100:+.0f}% · "
                     f"{a['mdd'] * 100:.0f}% · {a['is']['sharpe']:+.2f}|{a['oos']['sharpe']:+.2f}")
        if best is None or a["sharpe"] > best[1]["sharpe"]:
            best = (name, a)
    ew = bench_from_returns(ref["ew"], ppy)
    btc = [d for s, d in data.items() if s == "BTC-USDT"]
    lines.append("")
    lines.append("<b>Referencias</b>")
    lines.append(f"equiponderado largo: Sharpe {ew['sharpe']:+.2f} · {ew['cagr'] * 100:+.0f}%/año · maxDD {ew['mdd'] * 100:.0f}%")
    if btc:
        closes = [r[4] for r in btc[0]["rows"] if r[0] >= ref["curve"][0][0]]
        b = stats(closes, ppy)
        lines.append(f"BTC comprar y mantener: Sharpe {b['sharpe']:+.2f} · {b['cagr'] * 100:+.0f}%/año · maxDD {b['mdd'] * 100:.0f}%")
    name, a = best
    lines.append("")
    lines.append(f"<b>Mejor: {name.strip()}</b>")
    lines.append("por año: " + " · ".join(f"{y} {v * 100:+.0f}%" for y, v in sorted(a["years"].items())))
    lines.append(f"rotación {a['turn']:.1f}×/año · costes {a['cost'] * 100:.1f}%/año · funding {a['fund'] * 100:+.1f}%/año "
                 f"(positivo = pagado) · {a['npos']:.1f} pos. medias · bruta {a['gross']:.0%} · {a['trades']} entradas/salidas")
    lines.append("")
    lines.append(f"Lectura: ✅ = sobrevive a Bonferroni (t ≥ {BONF_T}); · = t ≥ 2 sin corregir. "
                 "Si el 30% final (fuera de muestra) cae a ≤0, la ventaja no es estable. "
                 "Universo = monedas con volumen HOY → sesgo de supervivencia a favor de los largos.")
    txt = "\n".join(lines)
    tg.send(txt)
    os.makedirs(C.DATA_DIR, exist_ok=True)
    out = os.path.join(C.DATA_DIR, f"investigacion_tsmom_{datetime.now(timezone.utc):%Y%m%d_%H%M}.txt")
    with open(out, "w") as f:
        import re
        f.write(re.sub(r"</?(b|code)>", "", txt))
        f.write("\n\nCurva de la variante de referencia (día, equity):\n")
        for t, e in ref["curve"]:
            f.write(f"{day(t)},{e:.2f}\n")
    tg.send_file(out)
    log.info("investigación terminada: %s", out)
    # el servicio de investigación se queda quieto en vez de reiniciarse y repetir
    while C._b("RESEARCH_HOLD", True):
        time.sleep(3600)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
