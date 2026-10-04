"""
Señal por activo, vela a vela, idéntica a la del Pine "TSMOM Convexo" (modo Días):
  tendencia [1][2] · volatilidad EWMA [1] · freno de liquidez [4] · carry por funding [3] · zona muerta [5]
Se usa igual en el bot y en el backtest.
"""
import math


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


class EWMAVar:
    """Varianza EWMA con arranque por media simple (evita quedarse pegada al primer dato)."""

    def __init__(self, com):
        self.com, self.a, self.v, self.k = com, 1.0 / (1.0 + com), None, 0

    def update(self, x2):
        self.k += 1
        if self.v is None:
            self.v = x2
        elif self.k <= self.com:
            self.v += (x2 - self.v) / self.k
        else:
            self.v = (1 - self.a) * self.v + self.a * x2
        return self.v


class AssetSignal:
    """Estado de un activo. update(close, funding_dia) en cada vela cerrada → dict de señal."""

    def __init__(self, p, bars_per_day=1.0, ann_days=365.0):
        self.p = p
        self.bpd = bars_per_day
        self.ann = ann_days * bars_per_day
        self.n = [max(1, round(h * bars_per_day)) for h in (p.H1, p.H2, p.H3)]
        self.w = [p.W1, p.W2, p.W3]
        self.closes = []
        self.vb = EWMAVar(p.VOL_COM * bars_per_day)
        self.vs = EWMAVar(p.VOL_SHORT * bars_per_day)
        self.vl = EWMAVar(p.VOL_LONG * bars_per_day)
        self.fund = []          # [(t_ms, tasa)] eventos de funding de los últimos FUND_DAYS días
        self.keep = max(self.n) + 5
        self.last = None

    def update(self, close, t_ms=None, funding=None):
        """close de la vela cerrada; t_ms = cierre de la vela; funding = [(t_ms, tasa)] cobrados en esa vela."""
        c1 = self.closes[-1] if self.closes else None
        self.closes.append(close)
        if len(self.closes) > self.keep:
            del self.closes[0]
        r = math.log(close / c1) if c1 and c1 > 0 and close > 0 else 0.0
        vb, vs, vl = self.vb.update(r * r), self.vs.update(r * r), self.vl.update(r * r)
        sbar = math.sqrt(vb)
        s_ann = sbar * math.sqrt(self.ann)
        ratio = math.sqrt(vs / vl) if vl > 0 else 1.0
        # [1][2] tendencia por horizonte
        parts, wsum = 0.0, 0.0
        comps = []
        for n, w in zip(self.n, self.w):
            if len(self.closes) > n and sbar > 0 and self.closes[-1 - n] > 0:
                ret = math.log(close / self.closes[-1 - n])
                s = math.copysign(1.0, ret) if self.p.SIGNAL_MODE == "signo" and ret != 0 else \
                    (0.0 if self.p.SIGNAL_MODE == "signo" else clip(ret / (sbar * math.sqrt(n)) / self.p.ZCAP))
                parts += w * s
                wsum += w
                comps.append(round(s, 3))
            else:
                comps.append(None)
        trend = parts / wsum if wsum > 0 else 0.0
        # [3] carry: funding positivo = largos pagan = largos amontonados → resta a los largos
        if funding:
            self.fund.extend(funding)
        if self.fund:
            ref = t_ms if t_ms is not None else self.fund[-1][0]
            lim = ref - self.p.FUND_DAYS * 86_400_000
            self.fund = [f for f in self.fund if f[0] > lim]
        carry = 0.0
        if self.p.USE_CARRY and self.fund:
            f_avg = sum(f[1] for f in self.fund) / len(self.fund) * 100.0   # % por periodo
            carry = clip(-f_avg / self.p.FUND_SCALE)
        comb0 = clip(trend + (self.p.CARRY_W * carry if self.p.USE_CARRY else 0.0))
        comb = 0.0 if abs(comb0) < self.p.DEAD_ZONE else comb0
        spiral = self.p.USE_LIQ and ratio > self.p.LIQ_TH
        throttle = min(1.0, self.p.LIQ_TH / max(ratio, 1e-9)) if self.p.USE_LIQ else 1.0
        self.last = {
            "close": close, "trend": round(trend, 4), "comps": comps, "carry": round(carry, 4),
            "comb": round(comb, 4), "sigma_ann": s_ann, "sigma_day": sbar * math.sqrt(self.bpd),
            "ratio": round(ratio, 3), "spiral": spiral, "throttle": throttle,
            "bars": len(self.closes), "convex": (vl - vs) * self.ann,
        }
        return self.last
