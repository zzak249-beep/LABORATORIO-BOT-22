"""
Cartera: de señales por activo a pesos objetivo, eventos legibles y cartera virtual.
Lo usan igual el bot (SIGNAL/LIVE) y el backtest, para que lo que se mide sea lo que se opera.

peso = señal × vol_objetivo / vol_activo × freno × escala   (fracción del equity, con signo)
"""
import math


def sgn(x):
    return (x > 0) - (x < 0)


def raw_weight(sig, p):
    if not sig or sig["sigma_ann"] <= 0 or sig["comb"] == 0:
        return 0.0
    scale = p.POS_SCALE if p.POS_SCALE > 0 else 1.0 / math.sqrt(max(1, p.MAX_POS))
    w = sig["comb"] * (p.TARGET_VOL / 100.0) / sig["sigma_ann"] * sig["throttle"] * scale
    if p.LONG_ONLY and w < 0:
        return 0.0
    return max(-p.MAX_W, min(p.MAX_W, w))


def targets(sigs, current, p, rebalance_day=True, allow_new=True):
    """
    sigs: {sym: señal} de los símbolos con datos HOY. current: {sym: peso actual}.
    Devuelve {sym: peso objetivo} (incluye los que pasan a 0).
    · Una posición se mantiene mientras la señal conserve el signo y salga de la zona muerta.
    · Las plazas libres se llenan con las señales más fuertes (|señal| ≥ MIN_SIGNAL).
    · Espiral de liquidez [4]: no se abre, no se aumenta ni se gira; solo se reduce o se cierra.
    · Banda [5]: ajustes pequeños de tamaño no se ejecutan.
    """
    tgt = {}
    held = {s: w for s, w in current.items() if abs(w) > 1e-12}
    # 1) posiciones abiertas
    for s, w in held.items():
        sig = sigs.get(s)
        if sig is None:                 # sin datos hoy (mercado TradFi en pausa, error): se deja como está
            tgt[s] = w
            continue
        rw = raw_weight(sig, p)
        if sgn(rw) != sgn(w):
            rw = 0.0 if (p.LIQ_FREEZE and sig["spiral"]) or sgn(rw) == 0 else rw   # giro (salvo en espiral: solo cierre)
        elif p.LIQ_FREEZE and sig["spiral"] and abs(rw) > abs(w):
            rw = w                      # congelado: no se aumenta
        tgt[s] = rw
    # 2) plazas libres → las señales más fuertes
    slots = p.MAX_POS - sum(1 for w in tgt.values() if abs(w) > 1e-12)
    if allow_new and slots > 0:
        cands = []
        for s, sig in sigs.items():
            if s in held or abs(sig["comb"]) < p.MIN_SIGNAL:
                continue
            if p.LIQ_FREEZE and sig["spiral"]:
                continue
            rw = raw_weight(sig, p)
            if abs(rw) > 1e-9:
                cands.append((abs(sig["comb"]), s, rw))
        cands.sort(reverse=True)
        for _, s, rw in cands[:slots]:
            tgt[s] = rw
    # 3) límite de exposición bruta
    gross = sum(abs(w) for w in tgt.values())
    if gross > p.GROSS_MAX > 0:
        k = p.GROSS_MAX / gross
        tgt = {s: w * k for s, w in tgt.items()}
    # 4) banda de no-operación [5] (no aplica a entradas, cierres ni giros)
    for s, w in list(tgt.items()):
        cur = held.get(s, 0.0)
        if cur == 0 or w == 0 or sgn(cur) != sgn(w):
            continue
        d = abs(w - cur)
        if not rebalance_day or d < p.BAND * abs(cur) or d < p.MIN_TRADE:
            tgt[s] = cur
    return tgt


def event_type(cur, new):
    if abs(cur) < 1e-12 and abs(new) > 1e-12:
        return "ENTRADA LARGO" if new > 0 else "ENTRADA CORTO"
    if abs(cur) > 1e-12 and abs(new) < 1e-12:
        return "CIERRE"
    if sgn(cur) != sgn(new):
        return "GIRO A LARGO" if new > 0 else "GIRO A CORTO"
    if abs(new) > abs(cur) + 1e-12:
        return "AUMENTA"
    if abs(new) < abs(cur) - 1e-12:
        return "REDUCE"
    return None


EMOJI = {"ENTRADA LARGO": "🟢", "ENTRADA CORTO": "🔴", "CIERRE": "⚪", "GIRO A LARGO": "🔄🟢",
         "GIRO A CORTO": "🔄🔴", "AUMENTA": "➕", "REDUCE": "➖"}


def changes(current, tgt):
    """Lista de (sym, evento, peso_actual, peso_nuevo) ordenada: cierres primero (liberan margen)."""
    out = []
    for s in set(current) | set(tgt):
        a, b = current.get(s, 0.0), tgt.get(s, 0.0)
        ev = event_type(a, b)
        if ev:
            out.append((s, ev, a, b))
    order = {"CIERRE": 0, "REDUCE": 1, "GIRO A LARGO": 2, "GIRO A CORTO": 2, "AUMENTA": 3,
             "ENTRADA LARGO": 4, "ENTRADA CORTO": 4}
    out.sort(key=lambda x: (order[x[1]], x[0]))
    return out


class Book:
    """Cartera virtual en unidades. mark() valora y cobra funding; rebalance() ejecuta con coste."""

    def __init__(self, equity=1000.0, cost_pct=0.08):
        self.equity = equity
        self.cost = cost_pct / 100.0
        self.units = {}       # sym → unidades con signo
        self.px = {}          # último precio conocido
        self.entry = {}       # sym → (precio medio de entrada, fecha)
        self.cost_paid = 0.0
        self.funding_paid = 0.0
        self.turnover = 0.0   # suma de |Δnocional| / equity

    def weights(self):
        if self.equity <= 0:
            return {}
        return {s: u * self.px.get(s, 0) / self.equity for s, u in self.units.items() if u}

    def mark(self, prices, funding=None):
        """prices: {sym: precio}. funding: {sym: suma de tasas cobradas desde la última marca}."""
        pnl = 0.0
        for s, u in self.units.items():
            p = prices.get(s)
            if p is None:
                continue
            pnl += u * (p - self.px.get(s, p))
            self.px[s] = p
            f = (funding or {}).get(s)
            if f:
                pay = u * p * f       # largo con funding positivo paga
                pnl -= pay
                self.funding_paid += pay
        for s, p in prices.items():
            if s not in self.units:
                self.px[s] = p
        self.equity += pnl
        return pnl

    def rebalance(self, tgt, prices, when=None):
        """Lleva los pesos a tgt. Devuelve lista de (sym, Δnocional)."""
        done = []
        eq = self.equity
        for s, w in tgt.items():
            p = prices.get(s) or self.px.get(s)
            if not p:
                continue
            u_new = w * eq / p
            u_old = self.units.get(s, 0.0)
            du = u_new - u_old
            if abs(du * p) < 1e-9:
                continue
            c = abs(du * p) * self.cost
            self.equity -= c
            self.cost_paid += c
            self.turnover += abs(du * p) / max(eq, 1e-9)
            if abs(u_new) < 1e-12:
                self.units.pop(s, None)
                self.entry.pop(s, None)
            else:
                if sgn(u_new) != sgn(u_old) or u_old == 0:
                    self.entry[s] = (p, when)
                elif abs(u_new) > abs(u_old):
                    e, d = self.entry.get(s, (p, when))
                    self.entry[s] = ((e * abs(u_old) + p * abs(du)) / abs(u_new), d)
                self.units[s] = u_new
            self.px[s] = p
            done.append((s, du * p))
        return done

    def to_dict(self):
        return {"equity": self.equity, "units": self.units, "px": self.px,
                "entry": {s: list(v) for s, v in self.entry.items()}, "cost_paid": self.cost_paid,
                "funding_paid": self.funding_paid, "turnover": self.turnover}

    @classmethod
    def from_dict(cls, d, cost_pct):
        b = cls(d.get("equity", 1000.0), cost_pct)
        b.units = {k: float(v) for k, v in d.get("units", {}).items()}
        b.px = {k: float(v) for k, v in d.get("px", {}).items()}
        b.entry = {k: tuple(v) for k, v in d.get("entry", {}).items()}
        b.cost_paid = d.get("cost_paid", 0.0)
        b.funding_paid = d.get("funding_paid", 0.0)
        b.turnover = d.get("turnover", 0.0)
        return b


# ── estadística común (bot y backtest) ──
def stats(eq_series, periods_per_year=365):
    """eq_series: lista de equity diario. Devuelve dict de métricas."""
    if len(eq_series) < 3:
        return {"n": len(eq_series)}
    r = [eq_series[i] / eq_series[i - 1] - 1 for i in range(1, len(eq_series)) if eq_series[i - 1] > 0]
    n = len(r)
    m = sum(r) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / max(1, n - 1))
    peak, mdd = eq_series[0], 0.0
    for e in eq_series:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1 if peak > 0 else 0)
    years = n / periods_per_year
    total = eq_series[-1] / eq_series[0] - 1 if eq_series[0] > 0 else 0
    cagr = (eq_series[-1] / eq_series[0]) ** (1 / years) - 1 if years > 0 and eq_series[-1] > 0 and eq_series[0] > 0 else -1
    return {"n": n, "total": total, "cagr": cagr, "vol": sd * math.sqrt(periods_per_year),
            "sharpe": (m / sd * math.sqrt(periods_per_year)) if sd > 0 else 0.0,
            "t": (m / sd * math.sqrt(n)) if sd > 0 else 0.0, "mdd": mdd}
