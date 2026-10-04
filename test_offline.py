"""Pruebas sin red: señal, cartera, un ciclo completo del bot en SIGNAL y el backtest, con datos sintéticos."""
import math
import os
import random
import shutil
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["TELEGRAM_TOKEN"] = ""
import config as C  # noqa: E402
import backtest  # noqa: E402
import data as D  # noqa: E402
import main as M  # noqa: E402
from portfolio import Book, changes, targets  # noqa: E402
from signals import AssetSignal  # noqa: E402

DAY = 86_400_000
random.seed(7)


def series(n, drift, vol, start=100.0):
    p, out = start, []
    for _ in range(n):
        p *= math.exp(drift + vol * random.gauss(0, 1))
        out.append(p)
    return out


def make(n=500):
    now = int(time.time() * 1000) // DAY * DAY
    t0 = now - n * DAY
    specs = {"UP-USDT": (0.008, 0.015), "DOWN-USDT": (-0.008, 0.015), "FLAT-USDT": (0.0, 0.03),
             "BTC-USDT": (0.001, 0.02), "NCFXEUR2USD-USDT": (0.0003, 0.005), "NEW-USDT": (0.01, 0.05)}
    out = {}
    for s, (d, v) in specs.items():
        m = 60 if s == "NEW-USDT" else n
        cl = series(m, d, v)
        rows = [[t0 + (n - m + i) * DAY, c, c, c, c, 1] for i, c in enumerate(cl)]
        fund = [(t0 + i * DAY // 3, 0.0003 if s == "UP-USDT" else -0.0001) for i in range(n * 3)]
        out[s] = {"rows": rows, "fund": fund}
    return out


DATA = make()


class FakeBX:
    contracts = {}

    def load_contracts(self):
        from universe import classify
        self.contracts = {s: {"cls": classify(s)[1], "api_open": True, "min_usdt": 2, "min_qty": 0} for s in DATA}
        return self.contracts

    def tickers(self):
        return [{"symbol": s, "quoteVolume": 1e9} for s in DATA]

    def klines_history(self, s, tf, total, tf_ms):
        return DATA[s]["rows"][-total:]

    def funding_history(self, s, start, end=None):
        return [f for f in DATA[s]["fund"] if f[0] >= start]


def test_signal():
    p = M.params()
    a, b = AssetSignal(p), AssetSignal(p)
    for x, y in zip(DATA["UP-USDT"]["rows"], DATA["DOWN-USDT"]["rows"]):
        ga, gb = a.update(x[4]), b.update(y[4])
    assert ga["trend"] > 0.3 and gb["trend"] < -0.3, (ga["trend"], gb["trend"])
    assert 0.15 < ga["sigma_ann"] < 0.45, ga["sigma_ann"]
    # el arranque de la EWMA no deja la espiral pegada (bug del Pine v1)
    c = AssetSignal(p)
    sp = [c.update(r[4])["spiral"] for r in DATA["FLAT-USDT"]["rows"]]
    assert sum(sp[150:]) / len(sp[150:]) < 0.2, sum(sp) / len(sp)
    print("señal OK", ga["comb"], gb["comb"])


def test_carry():
    p = M.params()
    rows, fund = DATA["UP-USDT"]["rows"], DATA["UP-USDT"]["fund"]
    g = D.replay("UP-USDT", rows, fund, p, "1d")
    assert g["carry"] < 0, g["carry"]   # funding positivo → resta al largo
    print("carry OK", g["carry"])


def test_portfolio():
    p = M.params()
    sigs = {"A": {"comb": 0.8, "sigma_ann": 0.6, "throttle": 1, "spiral": False},
            "B": {"comb": -0.5, "sigma_ann": 0.4, "throttle": 1, "spiral": False},
            "C": {"comb": 0.2, "sigma_ann": 0.5, "throttle": 1, "spiral": False},
            "D": {"comb": 0.9, "sigma_ann": 0.5, "throttle": 0.5, "spiral": True}}
    t = targets(sigs, {}, p)
    assert t["A"] > 0 and t["B"] < 0 and "C" not in t and "D" not in t, t
    # banda: un cambio pequeño no se ejecuta
    t2 = targets(sigs, {"A": t["A"] * 1.05, "B": t["B"]}, p)
    assert t2["A"] == t["A"] * 1.05
    # espiral: posición abierta no se aumenta
    t3 = targets(sigs, {"D": 0.01}, p)
    assert t3["D"] == 0.01, t3
    ev = [c[1] for c in changes({"A": 0.1, "B": -0.1}, {"A": 0, "B": 0.1, "E": 0.05})]
    assert ev == ["CIERRE", "GIRO A LARGO", "ENTRADA LARGO"], ev
    b = Book(1000, 0.1)
    b.rebalance({"X": 0.5}, {"X": 10})
    b.mark({"X": 11}, {"X": 0.001})
    assert abs(b.equity - (1000 - 0.5 + 50 - 0.55)) < 1e-6, b.equity
    print("cartera OK")


def test_bot_cycle():
    bot = M.Bot()
    bot.bx = FakeBX()
    bot.cycle(int(time.time() * 1000) // DAY * DAY)
    w = bot.book.weights()
    assert w.get("UP-USDT", 0) > 0 and w.get("DOWN-USDT", 0) < 0, w
    assert "NEW-USDT" not in w   # poca historia
    bot.cycle(int(time.time() * 1000) // DAY * DAY + DAY)
    assert bot.st["runs"] == 2 and bot.st["ranking"]
    print("ciclo bot OK", {k: round(v, 3) for k, v in w.items()})
    print(bot.verdict())


def test_backtest():
    p = M.params()
    r = backtest.simulate(DATA, p)
    a = backtest.summarize(r, 365)
    assert a and a["n"] > 200, a
    print(f"backtest OK · Sharpe {a['sharpe']:.2f} · t {a['t']:.2f} · pos {a['npos']:.1f} · rot {a['turn']:.1f}")


if __name__ == "__main__":
    test_signal()
    test_carry()
    test_portfolio()
    test_bot_cycle()
    test_backtest()
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
    print("TODO OK")
