"""Utilidades comunes de investigación: estadística honesta (agrupada por momento) y Bonferroni."""
import math
from collections import defaultdict

HOUR = 3_600_000
DAY = 86_400_000


def mean_sd(x):
    n = len(x)
    if n == 0:
        return 0.0, 0.0
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1)) if n > 1 else 0.0
    return m, sd


def tstat(x):
    m, sd = mean_sd(x)
    return m / (sd / math.sqrt(len(x))) if len(x) > 1 and sd > 0 else 0.0


def z_bonf(n_tests, alpha=0.05):
    """|t| necesario para p<alpha bilateral corrigiendo por n_tests (aprox. normal)."""
    p = alpha / (2 * max(1, n_tests))
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(mid / math.sqrt(2)) > p:
            lo = mid
        else:
            hi = mid
    return hi


def summarize(trades, key_fn):
    """trades: [{"t": ms, "ret": fracción neta, ...}]. key_fn agrupa los que ocurren a la vez (no son independientes).
    Devuelve n, media, mediana, % ganadoras, t ingenuo y t AGRUPADO (el que vale)."""
    if not trades:
        return {"n": 0}
    r = [x["ret"] for x in trades]
    groups = defaultdict(list)
    for x in trades:
        groups[key_fn(x)].append(x["ret"])
    g = [sum(v) / len(v) for v in groups.values()]
    rs = sorted(r)
    m = sum(r) / len(r)
    half = len(r) // 2
    cut = int(len(trades) * 0.7)
    ts = sorted(trades, key=lambda x: x["t"])
    first = [x["ret"] for x in ts[:cut]]
    last = [x["ret"] for x in ts[cut:]]
    return {"n": len(r), "groups": len(g), "mean": m, "median": rs[half], "win": sum(1 for v in r if v > 0) / len(r),
            "t_naive": tstat(r), "t": tstat(g) if len(g) > 2 else 0.0,
            "is": sum(first) / len(first) if first else 0.0, "oos": sum(last) / len(last) if last else 0.0}


def fmt_row(name, s, thr):
    if not s.get("n"):
        return f"<code>{name}</code> sin operaciones"
    star = " ✅" if s["t"] >= thr else (" ·" if s["t"] >= 2 else "")   # solo cuenta lo que GANA tras costes
    return (f"<code>{name}</code> n {s['n']} ({s['groups']} grp) · media {s['mean'] * 100:+.2f}% · "
            f"med {s['median'] * 100:+.2f}% · gana {s['win'] * 100:.0f}% · t {s['t']:+.2f}{star} · "
            f"70%|30% {s['is'] * 100:+.2f}|{s['oos'] * 100:+.2f}")


def funding_between(events, t0, t1):
    """Suma de tasas de funding cobradas con t0 < t ≤ t1 (fracción, no %)."""
    return sum(r for t, r in events if t0 < t <= t1)
