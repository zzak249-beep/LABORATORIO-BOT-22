"""
TSMOM-C bot · tendencia convexa + carry + freno de liquidez sobre TODAS las monedas de BingX.

Cada cierre de vela (por defecto diaria, 00:00 UTC + RUN_DELAY_MIN):
  1. escanea el universo (cripto + TradFi con liquidez), reconstruye la señal de cada símbolo
  2. elige la cartera (MAX_POS más fuertes, tamaño por volatilidad, freno, banda)
  3. SIGNAL: cartera virtual y avisos · LIVE: órdenes reales + stop de catástrofe
  4. Telegram: entradas/salidas marcadas con precio, tamaño, señal y stop; ranking de todas las monedas
RUN_MODE=research → backtest (backtest.py) y termina.
"""
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import config as C

logging.basicConfig(level=getattr(logging, C.LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("main")

from bingx import BingX, BingXError  # noqa: E402
from data import eligible, pick_universe, scan  # noqa: E402
from notify import Telegram  # noqa: E402
from portfolio import EMOJI, Book, changes, sgn, stats, targets  # noqa: E402
from universe import pretty  # noqa: E402

STATE_PATH = os.path.join(C.DATA_DIR, "tsmom_state.json")
EVENTS_PATH = os.path.join(C.DATA_DIR, "tsmom_events.csv")
EQUITY_PATH = os.path.join(C.DATA_DIR, "tsmom_equity.csv")
EV_FIELDS = ["time", "bar", "symbol", "event", "w_from", "w_to", "price", "notional", "signal", "trend",
             "carry", "sigma_ann", "ratio", "stop", "mode", "note"]


def params():
    return SimpleNamespace(**{k: getattr(C, k) for k in dir(C) if k.isupper()})


def utc(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_px(x):
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.6g}"


class Bot:
    def __init__(self):
        os.makedirs(C.DATA_DIR, exist_ok=True)
        self.p = params()
        self.bx = BingX(C.API_KEY, C.API_SECRET, C.VST)
        self.tg = Telegram(C.TG_TOKEN, C.TG_CHAT)
        self.st = self._load()
        self.book = Book.from_dict(self.st["book"], C.COST_PCT) if self.st.get("book") else Book(C.SIGNAL_EQUITY, C.COST_PCT)
        if not os.path.exists(EVENTS_PATH):
            with open(EVENTS_PATH, "w", newline="") as f:
                csv.writer(f).writerow(EV_FIELDS)
        if not os.path.exists(EQUITY_PATH):
            with open(EQUITY_PATH, "w", newline="") as f:
                csv.writer(f).writerow(["time", "bar", "equity", "btc", "gross", "n_pos", "mode"])

    # ── estado ──
    def _load(self):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"last_bar": 0, "runs": 0, "paused": False, "live": {}, "hist": [], "ranking": [], "book": None}

    def _save(self):
        self.st["book"] = self.book.to_dict()
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f)
        os.replace(tmp, STATE_PATH)

    def _event(self, bar, s, ev, a, b, px, notional, sig, stop=None, note=""):
        sig = sig or {}
        with open(EVENTS_PATH, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=EV_FIELDS).writerow({
                "time": utc(time.time() * 1000), "bar": utc(bar), "symbol": s, "event": ev,
                "w_from": round(a, 4), "w_to": round(b, 4), "price": px, "notional": round(notional, 2),
                "signal": sig.get("comb"), "trend": sig.get("trend"), "carry": sig.get("carry"),
                "sigma_ann": round(sig.get("sigma_ann", 0), 4), "ratio": sig.get("ratio"), "stop": stop,
                "mode": "LIVE" if C.LIVE else "SIGNAL", "note": note})

    # ── un ciclo ──
    def cycle(self, bar_close_ms):
        t0 = time.time()
        live_syms = list(self.st["live"].keys()) if C.LIVE else list(self.book.units.keys())
        syms, contracts = pick_universe(self.bx, extra=live_syms)
        sigs_all, errs = scan(self.bx, syms, self.p)
        sigs = eligible(sigs_all, self.p)
        bar = max((g["t"] for g in sigs_all.values()), default=bar_close_ms)
        prices = {s: g["close"] for s, g in sigs_all.items()}
        log.info("escaneo: %d símbolos, %d con señal, %d válidos, %d errores, %.0fs",
                 len(syms), len(sigs_all), len(sigs), len(errs), time.time() - t0)

        # ranking de TODAS las monedas (lo que pide "detectar todas")
        rank = sorted(((g["comb"], s) for s, g in sigs.items() if g["comb"] != 0), reverse=True)
        self.st["ranking"] = [[s, g, sigs[s]["trend"], sigs[s]["carry"], sigs[s]["spiral"]] for g, s in rank]

        # equity y pesos actuales
        if C.LIVE:
            eq, cur, notes = self._live_state(prices)
        else:
            fund = self._funding_since_last(sigs_all)
            self.book.mark(prices, fund)
            eq, cur, notes = self.book.equity, self.book.weights(), []

        prev_eq = self.st["hist"][-1][1] if self.st["hist"] else eq
        dd_day = (eq / prev_eq - 1) * 100 if prev_eq > 0 else 0
        allow_new = not self.st.get("paused") and dd_day > -C.MAX_DAILY_DD_PCT
        if dd_day <= -C.MAX_DAILY_DD_PCT:
            notes.append(f"⚠️ caída de {dd_day:.1f}% desde el último cierre: hoy no se abren posiciones nuevas")

        held_sigs = {s: sigs_all[s] for s in cur if s in sigs_all}
        rebal_day = self.st["runs"] % max(1, C.REBAL_DAYS) == 0
        tgt = targets({**sigs, **held_sigs}, cur, self.p, rebalance_day=rebal_day, allow_new=allow_new)
        chg = changes(cur, tgt)

        # ejecutar
        lines = []
        for s, ev, a, b in chg:
            sig = sigs_all.get(s)
            px = prices.get(s) or self.book.px.get(s)
            if not px:
                continue
            entry0 = (self.st["live"].get(s, {}).get("entry") if C.LIVE else (self.book.entry.get(s) or [None])[0])
            if C.LIVE:
                ok, info, stop = self._live_exec(s, ev, a, b, px, eq, sig, contracts.get(s, {}))
            else:
                self.book.rebalance({s: b}, prices, utc(bar))
                ok, info = True, ""
                stop = self._stop_price(b, px, sig) if b != 0 else None
            notional = (b - a) * eq
            self._event(bar, s, ev, a, b, px, notional, sig, stop, info)
            lines.append(self._fmt_change(s, ev, a, b, px, eq, sig, stop, ok, info, entry0))

        if C.LIVE:
            final_w = {s: v.get("qty", 0) * (prices.get(s) or 0) / eq for s, v in self.st["live"].items() if eq > 0}
        else:
            eq = self.book.equity
            final_w = self.book.weights()
        btc = prices.get("BTC-USDT")
        self.st["hist"].append([bar, eq, btc])
        self.st["hist"] = self.st["hist"][-2000:]
        self.st["runs"] += 1
        self.st["last_bar"] = bar_close_ms
        gross = sum(abs(w) for w in final_w.values())
        npos = sum(1 for w in final_w.values() if abs(w) > 1e-12)
        with open(EQUITY_PATH, "a", newline="") as f:
            csv.writer(f).writerow([utc(time.time() * 1000), utc(bar), round(eq, 2), btc, round(gross, 3), npos,
                                    "LIVE" if C.LIVE else "SIGNAL"])
        self._save()
        self._report(bar, syms, sigs, errs, lines, notes, eq, final_w, prices)
        if C.VERDICT_EVERY > 0 and self.st["runs"] % C.VERDICT_EVERY == 0:
            self.tg.send(self.verdict())

    def _funding_since_last(self, sigs_all):
        """Funding que paga/cobra la cartera virtual: tasas de la última vela (aprox. desde la marca anterior)."""
        out = {}
        for s in self.book.units:
            try:
                last = self.st.get("last_bar") or 0
                ev = self.bx.funding_history(s, last or int(time.time() * 1000) - 86_400_000)
                out[s] = sum(r for t, r in ev if t > last)
            except BingXError:
                pass
        return out

    def _stop_price(self, w, px, sig):
        if C.CAT_STOP_SIGMA <= 0 or not sig or w == 0:
            return None
        d = C.CAT_STOP_SIGMA * sig["sigma_day"] * px
        return px - d if w > 0 else px + d

    def _fmt_change(self, s, ev, a, b, px, eq, sig, stop, ok, info, entry0=None):
        name = pretty(s)
        sig = sig or {}
        comps = sig.get("comps") or []
        lab = ["1m", "3m", "12m"]
        cs = " ".join(f"{lab[i]} {c:+.2f}" for i, c in enumerate(comps) if c is not None)
        head = f"{EMOJI.get(ev, '•')} <b>{ev}</b> {name} @ {fmt_px(px)}"
        if ev == "CIERRE":
            e = entry0
            res = f" · resultado {((px / e - 1) * sgn(a)) * 100:+.1f}% (entrada {fmt_px(e)})" if e else ""
            body = f"   peso {a:+.0%} → 0{res}"
        else:
            body = (f"   peso {a:+.0%} → {b:+.0%} (≈{abs(b) * eq:,.0f} USDT nocional)"
                    f"\n   señal {sig.get('comb', 0):+.2f} = tendencia {sig.get('trend', 0):+.2f} [{cs}]"
                    f" + carry {sig.get('carry', 0):+.2f}"
                    f"\n   vol {sig.get('sigma_ann', 0) * 100:.0f}%/año · ratio vol {sig.get('ratio', 0):.2f}")
            if stop:
                body += f" · stop catástrofe {fmt_px(stop)}"
        if not ok:
            body += f"\n   ❌ NO ejecutada: {info}"
        elif info:
            body += f"\n   ℹ️ {info}"
        return head + "\n" + body

    def _report(self, bar, syms, sigs, errs, lines, notes, eq, tgt, prices):
        first = self.st["hist"][0][1] if self.st["hist"] else eq
        msg = [f"📊 <b>TSMOM-C</b> · cierre {utc(bar)} UTC ({C.TIMEFRAME}) · {'LIVE' if C.LIVE else 'SIGNAL'}",
               f"{len(syms)} monedas escaneadas · {len(sigs)} con historia suficiente"
               + (f" · {len(errs)} sin datos" if errs else "")]
        msg += notes
        msg.append("")
        msg += (["<b>Cambios</b>"] + lines) if lines else ["Sin cambios hoy (la banda evita operar por ruido)."]
        held = sorted(((abs(w), s, w) for s, w in tgt.items() if abs(w) > 1e-12), reverse=True)
        msg.append("")
        msg.append(f"<b>Cartera</b> ({len(held)}/{C.MAX_POS})")
        for _, s, w in held:
            e = (self.st["live"].get(s, {}).get("entry") if C.LIVE else (self.book.entry.get(s) or [None])[0])
            px = prices.get(s)
            pnl = f" · {((px / e - 1) * sgn(w)) * 100:+.1f}% desde {fmt_px(e)}" if e and px else ""
            msg.append(f"{'🟢' if w > 0 else '🔴'} {pretty(s)} {w:+.0%}{pnl}")
        msg.append(f"Equity {eq:,.2f} ({(eq / first - 1) * 100:+.1f}% desde inicio) · bruta {sum(h[0] for h in held):.0%}")
        r = self.st["ranking"]
        top_l = [f"{pretty(s)} {g:+.2f}" for s, g, *_ in r[:8] if g > 0]
        top_s = [f"{pretty(s)} {g:+.2f}" for s, g, *_ in r[::-1][:8] if g < 0]
        if top_l:
            msg.append("\n🔝 Más alcistas: " + " · ".join(top_l))
        if top_s:
            msg.append("🔻 Más bajistas: " + " · ".join(top_s))
        self.tg.send("\n".join(msg))

    # ── LIVE ──
    def _live_state(self, prices):
        notes = []
        eq_acc, _avail = self.bx.balance()
        eq = eq_acc * C.EQUITY_FRACTION
        pos = {}
        for p in self.bx.positions():
            s = p.get("symbol")
            amt = abs(float(p.get("positionAmt", 0) or 0))
            side = str(p.get("positionSide", "")).upper()
            if side == "SHORT" or (side == "BOTH" and float(p.get("positionAmt", 0)) < 0):
                amt = -amt
            pos[s] = pos.get(s, 0.0) + amt
        cur = {}
        for s, info in list(self.st["live"].items()):
            q = pos.get(s, 0.0)
            if abs(q) < 1e-12:
                notes.append(f"⚠️ {pretty(s)}: la posición ya no existe (¿stop de catástrofe o cierre manual?)")
                self._event(self.st["last_bar"], s, "CERRADA FUERA", info.get("qty", 0), 0, prices.get(s), 0, None,
                            note="sin posición en BingX")
                del self.st["live"][s]
                continue
            info["qty"] = q
            px = prices.get(s) or self.bx.price(s)
            cur[s] = q * px / eq if eq > 0 else 0
        return eq, cur, notes

    def _live_exec(self, s, ev, a, b, px, eq, sig, ct):
        info = self.st["live"].get(s, {"qty": 0.0})
        q_cur = info.get("qty", 0.0)
        q_tgt = b * eq / px
        min_usdt, min_qty = ct.get("min_usdt", 0) or 0, ct.get("min_qty", 0) or 0
        try:
            if ev in ("CIERRE", "GIRO A LARGO", "GIRO A CORTO") and q_cur:
                self._cancel_stops(s, q_cur > 0)
                self.bx.market_close(s, q_cur > 0, abs(q_cur))
                q_cur = 0.0
            if ev == "REDUCE":
                dq = abs(q_cur) - abs(q_tgt)
                if self.bx.fmt_qty(s, dq) <= 0:
                    return True, "reducción menor que el lote mínimo", None
                self.bx.market_close(s, q_cur > 0, dq)
                q_cur = sgn(q_cur) * (abs(q_cur) - self.bx.fmt_qty(s, dq))
            if ev in ("ENTRADA LARGO", "ENTRADA CORTO", "GIRO A LARGO", "GIRO A CORTO", "AUMENTA"):
                dq = abs(q_tgt) - abs(q_cur)
                if dq * px < max(min_usdt, 5) or self.bx.fmt_qty(s, dq) < min_qty or self.bx.fmt_qty(s, dq) <= 0:
                    if q_cur == 0:
                        self.st["live"].pop(s, None)
                    return False, f"tamaño {dq * px:.1f} USDT bajo el mínimo del contrato", None
                if q_cur == 0:
                    self.bx.set_margin_mode(s, C.MARGIN_MODE)
                    self.bx.set_leverage(s, C.LEVERAGE)
                cid = f"tsm{int(time.time() * 1000) % 10**12}"
                try:
                    self.bx.market_open(s, b > 0, dq, cid)
                except BingXError as e:
                    if "red" in str(e) and self.bx.order_exists(s, cid):
                        log.warning("%s: error de red pero la orden existe", s)
                    else:
                        raise
                q_cur = sgn(b) * (abs(q_cur) + self.bx.fmt_qty(s, dq))
                if ev.startswith("ENTRADA") or ev.startswith("GIRO"):
                    info["entry"] = px
                    info["since"] = utc(time.time() * 1000)
        except BingXError as e:
            log.error("%s %s: %s", s, ev, e)
            return False, str(e)[:160], None
        if abs(q_cur) < 1e-12 or b == 0:
            self.st["live"].pop(s, None)
            return True, "", None
        info["qty"] = q_cur
        self.st["live"][s] = info
        stop = self._stop_price(b, px, sig)
        if stop:
            try:
                self._cancel_stops(s, q_cur > 0)
                self.bx.exit_order(s, q_cur > 0, "STOP_MARKET", abs(q_cur), stop)
            except BingXError as e:
                return True, f"posición OK pero el stop falló: {str(e)[:120]}", stop
        return True, "", stop

    def _cancel_stops(self, s, side_long):
        try:
            for o in self.bx.stop_orders(s, side_long):
                cid = str(o.get("clientOrderId", o.get("clientOrderID", "")))
                if cid.startswith("tsm"):
                    self.bx.cancel(s, o.get("orderId"))
        except BingXError as e:
            log.warning("cancelar stops %s: %s", s, e)

    # ── veredicto ──
    def verdict(self):
        h = self.st["hist"]
        eqs = [x[1] for x in h if x[1]]
        btc = [x[2] for x in h if x[2]]
        ppy = 365 * C.bars_per_day()
        a = stats(eqs, ppy)
        b = stats(btc, ppy) if len(btc) == len(eqs) else {}
        if a.get("n", 0) < 10:
            return f"⚖️ Veredicto: solo {a.get('n', 0)} cierres, aún no hay nada que medir."
        t = a["t"]
        verdict = ("✅ ventaja estadística (t≥2)" if t >= 2 else "🟡 positivo pero sin significación (0<t<2)" if t > 0
                   else "🔴 negativo")
        msg = [f"⚖️ <b>Veredicto TSMOM-C</b> · {a['n']} cierres",
               f"cartera: {a['total'] * 100:+.1f}% · Sharpe {a['sharpe']:.2f} · t {t:+.2f} · maxDD {a['mdd'] * 100:.1f}% → {verdict}"]
        if b:
            msg.append(f"BTC mismo periodo: {b['total'] * 100:+.1f}% · Sharpe {b['sharpe']:.2f} · maxDD {b['mdd'] * 100:.1f}%")
        if not C.LIVE:
            msg.append(f"costes pagados {self.book.cost_paid:,.2f} · funding {self.book.funding_paid:+,.2f} · "
                       f"rotación {self.book.turnover:.1f}× equity")
        msg.append("TSMOM es una estrategia de meses: con menos de ~250 cierres diarios el t casi nunca llega a 2 aunque funcione.")
        return "\n".join(msg)

    # ── comandos ──
    def commands(self):
        for cmd in self.tg.poll():
            c = cmd.split()[0].split("@")[0].lower()
            if c in ("/estado", "/status"):
                nb = self.st["last_bar"] + C.tf_seconds() * 1000 + C.RUN_DELAY_MIN * 60_000
                self.tg.send(f"🤖 {C.CODE_VERSION}\n{C.summary()}\nciclos {self.st['runs']} · "
                             f"{'⏸ PAUSADO (no abre)' if self.st.get('paused') else '▶️ activo'}\n"
                             f"próximo cálculo {utc(nb)} UTC")
            elif c in ("/cartera", "/posiciones"):
                w = self.book.weights() if not C.LIVE else {s: v.get("qty", 0) for s, v in self.st["live"].items()}
                if not w:
                    self.tg.send("Cartera vacía.")
                else:
                    unit = "" if C.LIVE else "%"
                    self.tg.send("\n".join(f"{'🟢' if v > 0 else '🔴'} {pretty(s)} "
                                           f"{(v * 100 if not C.LIVE else v):+.1f}{unit}" for s, v in w.items()))
            elif c in ("/senales", "/señales", "/ranking"):
                r = self.st.get("ranking") or []
                if not r:
                    self.tg.send("Aún no hay escaneo.")
                    continue
                up = [f"{pretty(s)} {g:+.2f}{' 🌀' if sp else ''}" for s, g, _, _, sp in r if g > 0][:20]
                dn = [f"{pretty(s)} {g:+.2f}{' 🌀' if sp else ''}" for s, g, _, _, sp in r[::-1] if g < 0][:20]
                self.tg.send(f"🔝 <b>Alcistas</b> ({sum(1 for x in r if x[1] > 0)})\n" + "\n".join(up) +
                             f"\n\n🔻 <b>Bajistas</b> ({sum(1 for x in r if x[1] < 0)})\n" + "\n".join(dn) +
                             f"\n\nEntra con |señal| ≥ {C.MIN_SIGNAL:.2f} · 🌀 = espiral de liquidez (bloqueada)")
            elif c == "/pausa":
                self.st["paused"] = True
                self._save()
                self.tg.send("⏸ Pausado: no abre posiciones nuevas (las abiertas se siguen gestionando y cerrando).")
            elif c == "/reanudar":
                self.st["paused"] = False
                self._save()
                self.tg.send("▶️ Reanudado.")
            elif c == "/veredicto":
                self.tg.send(self.verdict())
            elif c in ("/ayuda", "/help", "/start"):
                self.tg.send("/estado · /cartera · /senales (ranking de todas las monedas) · /veredicto · /pausa · /reanudar")

    def run(self):
        self.tg.send(f"🚀 {C.CODE_VERSION}\n{C.summary()}\n"
                     + ("⚠️ LIVE: órdenes REALES" if C.LIVE else "SIGNAL: cartera virtual, sin órdenes")
                     + ("\n(MODE=LIVE pero falta CONFIRM_LIVE=SI → sigue en SIGNAL)" if C.MODE == "LIVE" and not C.LIVE else ""))
        if C.LIVE:
            try:
                self.bx.balance()
            except BingXError as e:
                self.tg.send(f"❌ No puedo leer la cuenta de BingX: {e}")
                raise SystemExit(1)
        tf_ms = C.tf_seconds() * 1000
        last_alive = 0
        while True:
            try:
                self.commands()
                now = int(time.time() * 1000)
                close = now // tf_ms * tf_ms                  # cierre de la última vela
                if close > self.st["last_bar"] and now >= close + C.RUN_DELAY_MIN * 60_000:
                    self.cycle(close)
                if time.time() - last_alive > 1800:
                    log.info("vivo · ciclos %d · próximo cierre %s UTC", self.st["runs"], utc(close + tf_ms))
                    last_alive = time.time()
            except BingXError as e:
                log.error("BingX: %s", e)
                time.sleep(60)
            except Exception as e:  # noqa: BLE001
                log.exception("error en el ciclo")
                self.tg.send(f"❌ error: {type(e).__name__}: {str(e)[:200]} (reintento en 5 min)")
                time.sleep(300)
            time.sleep(20)


if __name__ == "__main__":
    if C._s("RUN_MODE", "bot").lower() == "research":
        import backtest
        backtest.main()
    else:
        Bot().run()
