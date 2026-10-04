"""
Veredicto estadístico por sistema para KIBITO BOT (DEF / TREND / REBOTE).

Responde a una pregunta: ¿lo que estoy viendo es mala racha o el sistema NO rinde como su referencia?
Dos pruebas por sistema, ambas de una cola (¿peor que la referencia?):
  · aciertos: probabilidad binomial de acertar tan poco o menos si el acierto real fuera el de referencia
  · R medio: t de Student del R medio neto frente al R esperado de la referencia
    (R esperado de la referencia = (1 − acierto) × (PF − 1), suponiendo pérdidas de ~1R)
Y una tercera: ¿es rentable por sí mismo? (t del R medio frente a 0).

Uso dentro del bot:
    from veredicto import bloque
    txt = bloque({
        "DEF":    {"r": lista_R_netos_DEF,    "ref_wr": 0.35, "ref_pf": 1.47, "min_n": 30},
        "TREND":  {"r": lista_R_netos_TREND,  "ref_wr": 0.28, "ref_pf": 2.87, "min_n": 10},
        "REBOTE": {"r": lista_R_netos_REBOTE, "ref_wr": 0.59, "ref_pf": 1.99, "min_n": 10},
    })
    telegram.send(resumen + "\n\n" + txt)

Uso suelto con el diario:  python veredicto.py diario.csv   (columnas: system / sistema y r / r_net / R)
"""
import csv
import math
import sys


# ── distribuciones (sin scipy) ──
def binom_cdf(k, n, p):
    """P(X ≤ k) con X ~ Binomial(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    s = 0.0
    for i in range(k + 1):
        s += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                      + i * math.log(p) + (n - i) * math.log(1 - p))
    return min(1.0, s)


def _betacf(a, b, x):
    m_max, eps, fpmin = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > fpmin else fpmin)
    h = d
    for m in range(1, m_max + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > fpmin else fpmin)
        c = 1 + aa / c
        c = c if abs(c) > fpmin else fpmin
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > fpmin else fpmin)
        c = 1 + aa / c
        c = c if abs(c) > fpmin else fpmin
        de = d * c
        h *= de
        if abs(de - 1) < eps:
            break
    return h


def _ibeta(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def t_cdf(t, df):
    """P(T ≤ t) con T ~ Student(df)."""
    x = df / (df + t * t)
    tail = 0.5 * _ibeta(df / 2, 0.5, x)
    return 1 - tail if t > 0 else tail


# ── evaluación ──
def evaluar(nombre, r, ref_wr, ref_pf, min_n=30):
    r = [float(x) for x in r if x is not None]
    n = len(r)
    e_ref = (1 - ref_wr) * (ref_pf - 1)          # R esperado por operación según la referencia
    if n == 0:
        return {"nombre": nombre, "n": 0, "txt": f"<b>{nombre}</b>: sin operaciones cerradas"}
    wins = sum(1 for x in r if x > 0)
    mean = sum(r) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    p_wr = binom_cdf(wins, n, ref_wr)            # ¿tan pocos aciertos por azar?
    if n >= 3 and sd > 0:
        se = sd / math.sqrt(n)
        p_r = t_cdf((mean - e_ref) / se, n - 1)  # ¿R medio tan bajo por azar?
        t0 = mean / se
        p_pos = 1 - t_cdf(t0, n - 1)             # ¿rentable por sí mismo?
        n_need = math.ceil((2.0 * sd / e_ref) ** 2) if e_ref > 0 else None
    else:
        p_r, t0, p_pos, n_need = None, None, None, None
    p_min = min(x for x in (p_wr, p_r) if x is not None)

    if n < min_n and 0.01 <= p_min < 0.05:
        verdict, emoji = f"alarma temprana ({n}/{min_n} ops): va peor que la referencia; revisa costes y ejecución", "🟠"
    elif n < min_n and p_min >= 0.05:
        verdict, emoji = f"pocos datos ({n}/{min_n}): todavía es compatible con mala racha", "⚪"
    elif p_min < 0.01:
        verdict, emoji = "rinde PEOR que la referencia (casi seguro, no es mala suerte) → apágalo o revísalo", "🔴"
    elif p_min < 0.05:
        verdict, emoji = "rinde peor que la referencia (p<5 %) → revisa costes/ejecución antes de seguir", "🔴"
    elif p_min < 0.20:
        verdict, emoji = "por debajo de la referencia, aún dentro de lo posible → vigilar", "🟠"
    else:
        verdict, emoji = "compatible con la referencia", "🟢"
    if t0 is not None and t0 >= 2 and n >= min_n:
        verdict += " · ✅ rentable con significación"

    pr = "—" if p_r is None else f"{p_r * 100:.0f}%"
    txt = (f"{emoji} <b>{nombre}</b> · {n} ops · {wins / n * 100:.0f}% (ref {ref_wr * 100:.0f}%) · "
           f"R medio {mean:+.2f} (ref {e_ref:+.2f})\n"
           f"   prob. de verlo con un sistema sano: aciertos {p_wr * 100:.0f}% · R {pr}\n"
           f"   → {verdict}")
    if n_need and n < n_need:
        txt += f"\n   faltan ~{n_need - n} ops para distinguir este sistema de uno sin ventaja"
    return {"nombre": nombre, "n": n, "wins": wins, "mean": mean, "sd": sd, "e_ref": e_ref, "p_wr": p_wr,
            "p_r": p_r, "t0": t0, "p_pos": p_pos, "n_need": n_need, "p_min": p_min, "emoji": emoji, "txt": txt}


def bloque(sistemas):
    """sistemas: {nombre: {"r": [...], "ref_wr": .., "ref_pf": .., "min_n": ..}} → texto para Telegram."""
    out = ["⚖️ <b>Veredicto por sistema</b> (¿mala racha o no funciona?)"]
    for nombre, d in sistemas.items():
        out.append(evaluar(nombre, d.get("r", []), d["ref_wr"], d["ref_pf"], d.get("min_n", 30))["txt"])
    out.append("<i>prob. &lt;5 % = ya no es mala suerte · &gt;20 % = normal</i>")
    return "\n".join(out)


REFS = {"DEF": (0.35, 1.47, 30), "TREND": (0.28, 2.87, 10), "REBOTE": (0.59, 1.99, 10)}


def desde_csv(path):
    by = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            low = {k.lower(): v for k, v in row.items() if k}
            s = (low.get("system") or low.get("sistema") or low.get("strategy") or "").upper()
            v = low.get("r_net") or low.get("r") or low.get("pnl_r")
            if s and v not in (None, ""):
                try:
                    by.setdefault(s, []).append(float(v))
                except ValueError:
                    pass
    return {s: {"r": by.get(s, []), "ref_wr": wr, "ref_pf": pf, "min_n": mn} for s, (wr, pf, mn) in REFS.items()}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(bloque(desde_csv(sys.argv[1])))
    else:   # ejemplo con el resumen de hoy
        print(bloque({
            "DEF": {"r": [1.6] * 4 + [-1.0] * 10 + [-0.37] * 3, "ref_wr": 0.35, "ref_pf": 1.47, "min_n": 30},
            "TREND": {"r": [-1.02] * 6, "ref_wr": 0.28, "ref_pf": 2.87, "min_n": 10},
            "REBOTE": {"r": [0.5, 0.5, -1.0], "ref_wr": 0.59, "ref_pf": 1.99, "min_n": 10},
        }))
