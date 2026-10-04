"""Descarga y preparación de datos por símbolo (lo comparten bot y backtest)."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import config as C
from bingx import BingXError
from signals import AssetSignal
from universe import is_tradfi

log = logging.getLogger("data")


def pick_universe(bx, extra=()):
    """Todos los perpetuos USDT de BingX que pasan liquidez y categoría. extra = símbolos que se fuerzan (posiciones abiertas)."""
    contracts = bx.load_contracts()
    if C.SYMBOLS:
        syms = [s for s in C.SYMBOLS if s in contracts]
    else:
        vol = {}
        try:
            for t in bx.tickers():
                try:
                    vol[t["symbol"]] = float(t.get("quoteVolume", 0) or 0)
                except (TypeError, ValueError):
                    pass
        except BingXError as e:
            log.warning("tickers: %s", e)
        cats = set(C.CATEGORIES)
        syms = []
        for s, c in contracts.items():
            if s in C.BLACKLIST or not c.get("api_open", True):
                continue
            key = c["cls"]
            if key not in cats and not (key != "crypto" and "tradfi" in cats):
                continue
            th = C.MIN_QUOTE_VOL if key == "crypto" else C.MIN_QUOTE_VOL_TRADFI
            if vol.get(s, 0) >= th:
                syms.append(s)
        syms.sort(key=lambda s: -vol.get(s, 0))
        syms = syms[:C.MAX_SYMBOLS]
    for s in extra:
        if s not in syms and s in contracts:
            syms.append(s)
    return syms, contracts


def fetch(bx, sym, tf, bars, fund_days, now_ms=None):
    """Velas CERRADAS [t, o, h, l, c, v] + eventos de funding [(t, tasa)] de los últimos fund_days (+1) días."""
    now_ms = now_ms or int(time.time() * 1000)
    tf_ms = C.tf_seconds(tf) * 1000
    rows = bx.klines_history(sym, tf, bars + 1, tf_ms)
    rows = [r for r in rows if r[0] + tf_ms <= now_ms]          # fuera la vela en formación
    fund = []
    if C.USE_CARRY and rows:
        try:
            fund = bx.funding_history(sym, now_ms - (fund_days + 1) * 86_400_000)
        except BingXError as e:
            log.debug("funding %s: %s", sym, e)
    return rows[-bars:], fund


def replay(sym, rows, fund, p, tf):
    """Reconstruye la señal de un símbolo desde cero (sin estado: sobrevive a reinicios)."""
    bpd = C.bars_per_day(tf)
    sig = AssetSignal(p, bpd, 260.0 if is_tradfi(sym) else 365.0)
    tf_ms = C.tf_seconds(tf) * 1000
    fi = 0
    fund = sorted(fund)
    last = None
    for r in rows:
        close_t = r[0] + tf_ms
        ev = []
        while fi < len(fund) and fund[fi][0] <= close_t:
            ev.append(fund[fi])
            fi += 1
        last = sig.update(r[4], close_t, ev)
    if last is not None:
        last["t"] = rows[-1][0]
        last["days"] = len(rows) / bpd
    return last


def scan(bx, syms, p, tf=None, workers=8):
    """Señal de cada símbolo. Devuelve ({sym: señal}, {sym: error})."""
    tf = tf or C.TIMEFRAME
    out, errs = {}, {}

    def one(s):
        try:
            rows, fund = fetch(bx, s, tf, C.HISTORY_BARS, C.FUND_DAYS)
            if len(rows) < 3:
                return s, None, "sin velas"
            return s, replay(s, rows, fund, p, tf), None
        except BingXError as e:
            return s, None, str(e)
        except Exception as e:  # noqa: BLE001 — un símbolo raro no tumba el escaneo
            return s, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for s, sig, err in ex.map(one, syms):
            if sig is not None:
                out[s] = sig
            else:
                errs[s] = err
    return out, errs


def eligible(sigs, p):
    """Solo entran en el ranking los que tienen historia suficiente y vol válida."""
    return {s: g for s, g in sigs.items() if g.get("days", 0) >= p.MIN_HISTORY_DAYS and g["sigma_ann"] > 0}
