"""Pruebas sin red con un BingX falso: investigación de las 3 ideas, escaneo en vivo y motor en papel."""
import math
import os
import random
import shutil
import tempfile
import time
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["TELEGRAM_TOKEN"] = ""
os.environ["RESEARCH_DAYS"] = "200"
import config as C  # noqa: E402
import nv_funding as funding_clock
import nv_listing as listing
import nv_weekend as weekend  # noqa: E402
from nv_common import DAY, HOUR, z_bonf  # noqa: E402
from notify import Telegram  # noqa: E402
from engine import Engine  # noqa: E402
from universe import classify  # noqa: E402

M15 = 15 * 60_000
NOW = int(time.time() * 1000) // M15 * M15
random.seed(3)


def make(start, drift, vol, p0):
    out, p, t = [], p0, start
    while t < NOW:
        o = p
        p *= math.exp(drift + vol * random.gauss(0, 1))
        out.append([t, o, max(o, p) * 1.001, min(o, p) * 0.999, p, 100.0])
        t += M15
    return out


def agg(bars, step):
    out = {}
    for b in bars:
        k = b[0] // step * step
        if k not in out:
            out[k] = [k, b[1], b[2], b[3], b[4], b[5]]
        else:
            o = out[k]
            o[2], o[3], o[4], o[5] = max(o[2], b[2]), min(o[3], b[3]), b[4], o[5] + b[5]
    return [out[k] for k in sorted(out)]


SPEC = {"BTC-USDT": (NOW - 260 * DAY, 0.00002, 0.003, 60000),
        "OLD-USDT": (NOW - 260 * DAY, 0.0, 0.004, 2.0),
        "NEWA-USDT": (NOW - 100 * DAY + 5 * HOUR, -0.0004, 0.006, 1.0),
        "NEWB-USDT": (NOW - 40 * DAY + 13 * HOUR, -0.0004, 0.006, 0.5),
        "NEWC-USDT": (NOW - 24 * HOUR - 30 * 60_000, -0.0004, 0.006, 0.3),
        "NCCO724GOLD2USD-USDT": (NOW - 260 * DAY, 0.0, 0.0015, 2400.0)}
DATA = {}
for s, (st, d, v, p0) in SPEC.items():
    b15 = make(st // M15 * M15, d, v, p0)
    DATA[s] = {"15m": b15, "1h": agg(b15, HOUR), "1d": agg(b15, DAY)}
FUND = {s: [(t, (0.0012 if (t // (8 * HOUR)) % 3 == 0 else 0.0001) * (1 if s != "OLD-USDT" else -1))
            for t in range((NOW - 260 * DAY) // (8 * HOUR) * 8 * HOUR, NOW, 8 * HOUR)] for s in SPEC}


class FakeBX:
    contracts = {}

    def load_contracts(self):
        self.contracts = {s: {"cls": classify(s)[1], "api_open": True, "min_usdt": 2, "min_qty": 0} for s in SPEC}
        return self.contracts

    def tickers(self):
        return [{"symbol": s, "lastPrice": DATA[s]["15m"][-1][4], "quoteVolume": 1e9} for s in SPEC]

    def price(self, s):
        return DATA[s]["15m"][-1][4]

    def klines(self, s, interval, limit=1000, end_time=None, start_time=None):
        b = DATA[s][interval]
        if start_time is not None:
            b = [x for x in b if x[0] >= start_time]
            if end_time is not None:
                b = [x for x in b if x[0] <= end_time]
            return b[:limit]
        if end_time is not None:
            b = [x for x in b if x[0] <= end_time]
        return b[-limit:]

    def klines_history(self, s, interval, total, tf_ms):
        return DATA[s][interval][-total:]

    def funding_history(self, s, start, end=None):
        return [f for f in FUND[s] if f[0] >= start and (end is None or f[0] <= end)]

    def premium_all(self):
        nxt = (NOW // (8 * HOUR) + 1) * 8 * HOUR
        return {s: (0.0012, nxt, self.price(s)) for s in SPEC}


def test_research():
    tg = Telegram("", "")
    bx = FakeBX()
    r1 = listing.research(bx, tg)
    assert "2 listados" in r1, r1[:300]
    r2 = funding_clock.research(bx, tg)
    assert "cobros" in r2 and "pre" in r2
    r3 = weekend.research(bx, tg)
    assert "1 TradFi" in r3, r3[:300]
    print(r1, "\n\n", r2, "\n\n", r3)


def test_bonf():
    assert abs(z_bonf(1) - 1.96) < 0.01 and abs(z_bonf(12) - 2.87) < 0.02, (z_bonf(1), z_bonf(12))


def test_listing_live():
    bx = FakeBX()
    st = {}
    now = NOW
    plans = listing.scan(bx, st, now, bx.load_contracts())
    assert [p["symbol"] for p in plans] == ["NEWC-USDT"], plans
    assert listing.scan(bx, st, now, bx.contracts) == []          # no repite
    assert st["lst"]["BTC-USDT"] == 0
    print("listing en vivo OK", plans[0]["why"])


def test_funding_live():
    bx = FakeBX()
    nxt = (NOW // (8 * HOUR) + 1) * 8 * HOUR
    st = {}
    plans = funding_clock.scan(bx, st, nxt - C.FC_WIN_MIN * 60_000 + 60_000, {s: 1e9 for s in SPEC})
    assert plans and all(p["side"] == -1 for p in plans), plans
    assert not funding_clock.scan(bx, st, nxt - C.FC_WIN_MIN * 60_000 + 120_000, {s: 1e9 for s in SPEC})
    print("funding en vivo OK", len(plans))


def test_weekend_live():
    bx = FakeBX()
    # domingo 21:05 UTC más reciente con datos
    t = NOW
    while True:
        d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        if d.weekday() == 6 and d.hour == 21 and d.minute == 0:
            break
        t -= M15
    st = {}
    os.environ["WK_Z"] = "0"
    C.WK_Z = 0.0
    plans = weekend.scan(bx, st, t + 5 * 60_000, bx.load_contracts())
    assert len(plans) == 1 and plans[0]["symbol"].startswith("NCCO724"), plans
    assert weekend.scan(bx, st, t + 6 * 60_000, bx.contracts) == []
    print("weekend en vivo OK", plans[0]["why"])


def test_engine():
    bx = FakeBX()
    bx.load_contracts()
    eng = Engine(bx, Telegram("", ""))
    eng.contracts = bx.contracts
    px = bx.price("NEWB-USDT")
    eng.open({"module": "listing", "symbol": "NEWB-USDT", "side": -1, "stop": px * 1.2, "close_at": NOW + DAY,
              "hedge": True, "why": "test"}, {})
    eng.open({"module": "listing", "symbol": "NEWB-USDT", "side": -1, "stop": px * 1.2, "close_at": NOW + DAY}, {})
    assert len(eng.st["open"]) == 1
    p = eng.st["open"][0]
    assert abs(p["notional"] - 10000 * 0.005 / 0.2) < 1e-6, p["notional"]
    eng.monitor({"NEWB-USDT": px * 1.25}, NOW)               # salta el stop
    assert not eng.st["open"] and eng.st["closed"][-1]["r"] < -0.9, eng.st["closed"]
    eng.open({"module": "funding", "symbol": "OLD-USDT", "side": 1, "stop_pct": 1.5, "close_at": NOW - 1, "why": "t"}, {})
    eng.monitor({"OLD-USDT": bx.price("OLD-USDT")}, NOW)     # cierre por tiempo
    assert len(eng.st["closed"]) == 2
    print(eng.stats_text())


if __name__ == "__main__":
    test_bonf()
    test_research()
    test_listing_live()
    test_funding_live()
    test_weekend_live()
    test_engine()
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
    print("TODO OK")
