"""
Motor común: convierte los planes de las ideas en operaciones (papel o reales), las vigila y las cierra.
Todas las ideas cierran por TIEMPO o por STOP, así que el motor es el mismo para las tres.
"""
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone

import config as C
from bingx import BingXError
from ideas.common import tstat
from universe import pretty

log = logging.getLogger("engine")
STATE = os.path.join(C.DATA_DIR, "nova_state.json")
JOURNAL = os.path.join(C.DATA_DIR, "nova_trades.csv")
FIELDS = ["open_time", "close_time", "module", "symbol", "side", "entry", "exit", "stop", "notional", "ret_pct",
          "hedge_ret_pct", "funding_pct", "pnl", "r", "why_exit", "why_entry", "mode"]
MAXOPEN = {"listing": C.LST_MAX_OPEN, "funding": C.FC_MAX_OPEN, "weekend": C.WK_MAX_OPEN}
NAMES = {"listing": "LISTING", "funding": "FUNDING", "weekend": "WEEKEND"}


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d/%m %H:%M")


def fpx(x):
    return f"{x:,.2f}" if x >= 100 else f"{x:.4f}" if x >= 1 else f"{x:.6g}"


class Engine:
    def __init__(self, bx, tg):
        os.makedirs(C.DATA_DIR, exist_ok=True)
        self.bx, self.tg = bx, tg
        try:
            with open(STATE) as f:
                self.st = json.load(f)
        except (OSError, ValueError):
            self.st = {}
        self.st.setdefault("open", [])
        self.st.setdefault("closed", [])
        self.st.setdefault("equity", C.SIGNAL_EQUITY)
        self.st.setdefault("paused", False)
        self.st.setdefault("ideas", {})
        if not os.path.exists(JOURNAL):
            with open(JOURNAL, "w", newline="") as f:
                csv.writer(f).writerow(FIELDS)
        self.contracts = {}

    def save(self):
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f)
        os.replace(tmp, STATE)

    def equity(self):
        if C.LIVE:
            try:
                return self.bx.balance()[0]
            except BingXError:
                pass
        return self.st["equity"]

    # ── abrir ──
    def open(self, plan, prices):
        s, mod = plan["symbol"], plan["module"]
        opn = self.st["open"]
        if self.st["paused"]:
            return
        if any(p["symbol"] == s for p in opn):
            return
        if len(opn) >= C.MAX_OPEN or sum(1 for p in opn if p["module"] == mod) >= MAXOPEN.get(mod, 4):
            log.info("%s %s: sin plaza", mod, s)
            return
        try:
            px = prices.get(s) or self.bx.price(s)
        except BingXError as e:
            log.warning("precio %s: %s", s, e)
            return
        side = plan["side"]
        stop = plan.get("stop") or px * (1 - side * plan["stop_pct"] / 100)
        dist = abs(px - stop) / px
        if dist <= 0.001 or (side > 0 and stop >= px) or (side < 0 and stop <= px):
            return
        eq = self.equity()
        notional = min(eq * C.RISK_PCT / 100 / dist, eq * C.MAX_NOTIONAL_PCT / 100)
        qty = notional / px
        pos = {"id": f"{mod}-{s}-{int(time.time())}", "module": mod, "symbol": s, "side": side, "entry": px,
               "qty": qty, "notional": notional, "stop": stop, "close_at": plan["close_at"], "opened": int(time.time() * 1000),
               "why": plan.get("why", ""), "risk_usd": eq * C.RISK_PCT / 100, "hedge": None}
        if plan.get("hedge"):
            try:
                bpx = prices.get("BTC-USDT") or self.bx.price("BTC-USDT")
                pos["hedge"] = {"entry": bpx, "qty": notional / bpx}
            except BingXError:
                pass
        if C.LIVE and not self._live_open(pos):
            return
        opn.append(pos)
        self.save()
        hold = (pos["close_at"] - pos["opened"]) / 3_600_000
        self.tg.send(
            f"{'🟢' if side > 0 else '🔴'} <b>ENTRADA {'LARGO' if side > 0 else 'CORTO'}</b> · {NAMES[mod]} · "
            f"<b>{pretty(s)}</b> @ {fpx(px)}\n"
            f"   stop {fpx(stop)} ({dist * 100:.1f}%) · cierre {utc(pos['close_at'])} UTC ({hold:.0f} h)\n"
            f"   tamaño {notional:,.0f} USDT · riesgo {C.RISK_PCT}% = {pos['risk_usd']:,.0f} USDT"
            + (f" · cubierto con largo BTC {notional:,.0f}" if pos["hedge"] else "")
            + f"\n   {pos['why']}" + ("" if C.LIVE else "\n   (papel)"))

    def _live_open(self, pos):
        s, c = pos["symbol"], self.contracts.get(pos["symbol"], {})
        if pos["notional"] < max(c.get("min_usdt", 0) or 0, 5) or self.bx.fmt_qty(s, pos["qty"]) < (c.get("min_qty") or 0):
            log.info("%s: tamaño bajo el mínimo", s)
            return False
        try:
            self.bx.set_margin_mode(s, C.MARGIN_MODE)
            self.bx.set_leverage(s, C.LEVERAGE)
            cid = f"nva{int(time.time() * 1000) % 10**12}"
            try:
                self.bx.market_open(s, pos["side"] > 0, pos["qty"], cid, stop_loss=pos["stop"])
            except BingXError as e:
                if not ("red" in str(e) and self.bx.order_exists(s, cid)):
                    raise
            pos["qty"] = self.bx.fmt_qty(s, pos["qty"])
            if pos["hedge"]:
                q = self.bx.fmt_qty("BTC-USDT", pos["hedge"]["qty"])
                self.bx.market_open("BTC-USDT", True, q, f"nvah{int(time.time() * 1000) % 10**11}")
                pos["hedge"]["qty"] = q
            return True
        except BingXError as e:
            self.tg.send(f"❌ {pretty(s)}: no se pudo abrir ({str(e)[:150]})")
            return False

    # ── vigilar ──
    def monitor(self, prices, now):
        if not self.st["open"]:
            return
        live_pos = None
        if C.LIVE:
            try:
                live_pos = {}
                for p in self.bx.positions():
                    amt = float(p.get("positionAmt", 0) or 0)
                    ps = str(p.get("positionSide", "")).upper()
                    side = -1 if ps == "SHORT" or (ps == "BOTH" and amt < 0) else 1
                    live_pos[(p["symbol"], side)] = abs(amt)
            except BingXError as e:
                log.warning("posiciones: %s", e)
                live_pos = None
        for pos in list(self.st["open"]):
            s, d = pos["symbol"], pos["side"]
            px = prices.get(s)
            if C.LIVE and live_pos is not None and (s, d) not in live_pos:
                self.close(pos, px or pos["stop"], "STOP", already_closed=True)
                continue
            if px is None:
                continue
            if not C.LIVE and ((d > 0 and px <= pos["stop"]) or (d < 0 and px >= pos["stop"])):
                self.close(pos, px, "STOP")
            elif now >= pos["close_at"]:
                self.close(pos, px, "TIEMPO")

    def close(self, pos, px, why, already_closed=False):
        s, d = pos["symbol"], pos["side"]
        if C.LIVE and not already_closed:
            try:
                self.bx.market_close(s, d > 0, pos["qty"])
                for o in self.bx.stop_orders(s, d > 0):
                    self.bx.cancel(s, o.get("orderId"))
            except BingXError as e:
                self.tg.send(f"❌ {pretty(s)}: fallo al cerrar ({str(e)[:150]}) — reviso en el próximo ciclo")
                return
        if C.LIVE and pos["hedge"]:
            try:
                self.bx.market_close("BTC-USDT", True, pos["hedge"]["qty"])
            except BingXError as e:
                self.tg.send(f"❌ cobertura BTC no cerrada: {str(e)[:120]}")
        cost = C.COST_PCT / 100
        try:
            fev = self.bx.funding_history(s, pos["opened"])
            fund = -d * sum(r for t, r in fev if t > pos["opened"])
        except BingXError:
            fund = 0.0
        ret = d * (px / pos["entry"] - 1) - 2 * cost + fund
        pnl = pos["notional"] * ret
        hret = None
        if pos["hedge"]:
            try:
                bpx = self.bx.price("BTC-USDT")
                hret = bpx / pos["hedge"]["entry"] - 1 - 2 * cost
                pnl += pos["notional"] * hret
            except BingXError:
                pass
        r = pnl / pos["risk_usd"] if pos["risk_usd"] else 0.0
        if not C.LIVE:
            self.st["equity"] += pnl
        self.st["open"] = [p for p in self.st["open"] if p["id"] != pos["id"]]
        self.st["closed"].append({"m": pos["module"], "s": s, "r": round(r, 4), "ret": round(ret, 6),
                                  "t": int(time.time() * 1000)})
        self.st["closed"] = self.st["closed"][-5000:]
        self.save()
        with open(JOURNAL, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow({
                "open_time": utc(pos["opened"]), "close_time": utc(time.time() * 1000), "module": pos["module"],
                "symbol": s, "side": d, "entry": pos["entry"], "exit": px, "stop": pos["stop"],
                "notional": round(pos["notional"], 2), "ret_pct": round(ret * 100, 4),
                "hedge_ret_pct": None if hret is None else round(hret * 100, 4), "funding_pct": round(fund * 100, 4),
                "pnl": round(pnl, 2), "r": round(r, 3), "why_exit": why, "why_entry": pos["why"],
                "mode": "LIVE" if C.LIVE else "PAPEL"})
        emo = "✅" if r > 0.05 else "❌" if r < -0.05 else "➖"
        self.tg.send(f"{emo} <b>CIERRE {why}</b> · {NAMES[pos['module']]} · {pretty(s)} @ {fpx(px)}\n"
                     f"   {'largo' if d > 0 else 'corto'} desde {fpx(pos['entry'])} · {ret * 100:+.2f}% neto"
                     + (f" · cobertura BTC {hret * 100:+.2f}%" if hret is not None else "")
                     + f" · funding {fund * 100:+.3f}%\n   resultado <b>{r:+.2f}R</b> ({pnl:+,.2f} USDT)")

    # ── estadística ──
    def stats_text(self):
        out = [f"📊 <b>NOVA</b> · {'LIVE' if C.LIVE else 'PAPEL'} · equity {self.equity():,.2f}"]
        for m in ("listing", "funding", "weekend"):
            rs = [x["r"] for x in self.st["closed"] if x["m"] == m]
            if not rs:
                out.append(f"{NAMES[m]}: sin operaciones cerradas")
                continue
            t = tstat(rs)
            w = sum(1 for x in rs if x > 0) / len(rs)
            gw = sum(x for x in rs if x > 0)
            gl = -sum(x for x in rs if x < 0)
            verdict = ("✅ ventaja (t≥2)" if t >= 2 and len(rs) >= 30 else "🔴 sin ventaja, va negativo" if t <= -2
                       else "⚪ aún sin conclusión" if len(rs) < 30 else "🟡 sin significación")
            out.append(f"{NAMES[m]}: {len(rs)} ops · acierto {w * 100:.0f}% · PF {gw / gl if gl else float('inf'):.2f} · "
                       f"{sum(rs):+.1f}R · media {sum(rs) / len(rs):+.2f}R · t {t:+.2f} → {verdict}")
        if self.st["open"]:
            out.append("abiertas: " + ", ".join(f"{pretty(p['symbol'])} ({NAMES[p['module']][:3]})" for p in self.st["open"]))
        return "\n".join(out)
