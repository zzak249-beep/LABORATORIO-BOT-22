"""
NOVA bot · tres ideas estructurales que casi nadie opera, sobre todos los perpetuos de BingX:
  1. LISTING  — corto en monedas recién listadas (cubierto con BTC)
  2. FUNDING  — operar el reloj del funding extremo (antes/después del cobro)
  3. WEEKEND  — corregir el movimiento de fin de semana de los perpetuos TradFi 24/7
RUN_MODE=research → investiga las tres con datos de BingX y manda el informe. RUN_MODE=bot → opera (papel por defecto).
"""
import logging
import sys
import time
from datetime import datetime, timezone

import config as C

logging.basicConfig(level=getattr(logging, C.LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("main")

from bingx import BingX, BingXError  # noqa: E402
from engine import Engine  # noqa: E402
import nv_funding as funding_clock
import nv_listing as listing
import nv_weekend as weekend  # noqa: E402
from notify import Telegram  # noqa: E402
from universe import pretty  # noqa: E402


class Bot:
    def __init__(self):
        self.bx = BingX(C.API_KEY, C.API_SECRET, C.VST)
        self.tg = Telegram(C.TG_TOKEN, C.TG_CHAT)
        self.eng = Engine(self.bx, self.tg)
        self.prices, self.vol = {}, {}
        self.t_tick = self.t_fund = self.t_list = self.t_week = self.t_alive = 0

    def tickers(self):
        p, v = {}, {}
        for t in self.bx.tickers():
            try:
                p[t["symbol"]] = float(t["lastPrice"])
                v[t["symbol"]] = float(t.get("quoteVolume", 0) or 0)
            except (KeyError, TypeError, ValueError):
                pass
        if p:
            self.prices, self.vol = p, v

    def commands(self):
        for cmd in self.tg.poll():
            c = cmd.split()[0].split("@")[0].lower()
            if c in ("/estado", "/stats", "/status"):
                self.tg.send(self.eng.stats_text() + f"\n{C.CODE_VERSION} · {C.summary()}"
                             + ("\n⏸ PAUSADO" if self.eng.st["paused"] else ""))
            elif c == "/abiertas":
                o = self.eng.st["open"]
                self.tg.send("\n".join(
                    f"{'🟢' if p['side'] > 0 else '🔴'} {pretty(p['symbol'])} · {p['module']} · entrada {p['entry']:.6g} · "
                    f"ahora {self.prices.get(p['symbol'], 0):.6g} · stop {p['stop']:.6g}" for p in o) or "Nada abierto.")
            elif c == "/pausa":
                self.eng.st["paused"] = True
                self.eng.save()
                self.tg.send("⏸ Pausado: no abre nada nuevo; lo abierto se cierra según su plan.")
            elif c == "/reanudar":
                self.eng.st["paused"] = False
                self.eng.save()
                self.tg.send("▶️ Reanudado.")
            elif c in ("/ayuda", "/help", "/start"):
                self.tg.send("/estado · /abiertas · /pausa · /reanudar")

    def step(self):
        now = int(time.time() * 1000)
        t = time.time()
        if t - self.t_tick >= 30:
            self.tickers()
            self.t_tick = t
            self.eng.monitor(self.prices, now)
        plans = []
        if "funding" in C.MODULES and t - self.t_fund >= 60:
            plans += funding_clock.scan(self.bx, self.eng.st["ideas"], now, self.vol)
            self.t_fund = t
        if "listing" in C.MODULES and t - self.t_list >= 900:
            self.eng.contracts = self.bx.load_contracts()
            plans += listing.scan(self.bx, self.eng.st["ideas"], now, self.eng.contracts)
            self.t_list = t
        if "weekend" in C.MODULES and t - self.t_week >= 300:
            if not self.eng.contracts:
                self.eng.contracts = self.bx.load_contracts()
            plans += weekend.scan(self.bx, self.eng.st["ideas"], now, self.eng.contracts)
            self.t_week = t
        for p in plans:
            self.eng.open(p, self.prices)
        if plans:
            self.eng.save()
        dt = datetime.now(timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        if dt.hour == 20 and self.eng.st.get("last_report") != day:
            self.eng.st["last_report"] = day
            self.eng.save()
            self.tg.send(self.eng.stats_text())
        if t - self.t_alive > 1800:
            log.info("vivo · %d abiertas · %d cerradas", len(self.eng.st["open"]), len(self.eng.st["closed"]))
            self.t_alive = t

    def run(self):
        self.tg.send(f"🚀 {C.CODE_VERSION}\n{C.summary()}\n"
                     + ("⚠️ LIVE: órdenes REALES" if C.LIVE else "PAPEL: sin órdenes reales")
                     + ("\n(MODE=LIVE pero falta CONFIRM_LIVE=SI → sigue en papel)" if C.MODE == "LIVE" and not C.LIVE else "")
                     + "\nPrimera pasada: reviso la fecha de listado de todas las monedas (1–2 min).")
        if C.LIVE:
            try:
                self.bx.balance()
            except BingXError as e:
                self.tg.send(f"❌ No puedo leer la cuenta: {e}")
                raise SystemExit(1)
        while True:
            try:
                self.commands()
                self.step()
            except BingXError as e:
                log.error("BingX: %s", e)
                time.sleep(30)
            except Exception as e:  # noqa: BLE001
                log.exception("error")
                self.tg.send(f"❌ error: {type(e).__name__}: {str(e)[:200]} (sigo en 2 min)")
                time.sleep(120)
            time.sleep(10)


if __name__ == "__main__":
    if C.RUN_MODE == "research":
        import research
        research.main()
    else:
        Bot().run()
