"""NOVA bot · configuración por variables de entorno (se quitan comillas: lección de Railway)."""
import os

CODE_VERSION = "nova-bot 1.0.1 (2026-10-04)"


def _raw(n, d):
    v = os.getenv(n)
    if v is None:
        return d
    v = v.strip().strip('"').strip("'").strip()
    return d if v == "" else v


def _s(n, d):
    return str(_raw(n, d))


def _f(n, d):
    try:
        return float(_raw(n, d))
    except (TypeError, ValueError):
        return float(d)


def _i(n, d):
    try:
        return int(float(_raw(n, d)))
    except (TypeError, ValueError):
        return int(d)


def _b(n, d):
    return str(_raw(n, d)).lower() in ("1", "true", "si", "sí", "yes", "on")


def _list(n, d=""):
    return [x.strip().lower() for x in _s(n, d).split(",") if x.strip()]


RUN_MODE = _s("RUN_MODE", "bot").lower()          # bot | research
MODE = _s("MODE", "SIGNAL").upper()
CONFIRM_LIVE = _s("CONFIRM_LIVE", "NO").upper()
LIVE = MODE == "LIVE" and CONFIRM_LIVE in ("SI", "SÍ", "YES")
VST = _b("BINGX_VST", False)
API_KEY = _s("BINGX_API_KEY", "")
API_SECRET = _s("BINGX_API_SECRET", "")
TG_TOKEN = _s("TELEGRAM_TOKEN", "") or _s("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = _s("TELEGRAM_CHAT_ID", "")
DATA_DIR = _s("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
LOG_LEVEL = _s("LOG_LEVEL", "INFO")

MODULES = _list("MODULES", "listing,funding,weekend")   # qué ideas corren en el bot

# ── riesgo común ──
RISK_PCT = _f("RISK_PCT", 0.5)                  # % del equity arriesgado por operación (hasta el stop)
MAX_NOTIONAL_PCT = _f("MAX_NOTIONAL_PCT", 30)   # nocional máximo por operación (% equity)
MAX_OPEN = _i("MAX_OPEN", 8)                    # operaciones abiertas a la vez (todas las ideas)
LEVERAGE = _i("LEVERAGE", 5)
MARGIN_MODE = _s("MARGIN_MODE", "ISOLATED").upper()
COST_PCT = _f("COST_PCT", 0.07)                 # % por lado (comisión taker 0.05 + deslizamiento)
SIGNAL_EQUITY = _f("SIGNAL_EQUITY", 10000)
MIN_QUOTE_VOL = _f("MIN_QUOTE_VOL", 3_000_000)
BLACKLIST = [x.upper() for x in _list("BLACKLIST", "")]

# ── IDEA 1 · LISTING: las monedas recién listadas tienden a caer (desbloqueos, airdrops, sin compradores) ──
LST_DELAY_H = _i("LST_DELAY_H", 24)             # horas tras el listado antes de entrar en corto
LST_HOLD_D = _f("LST_HOLD_D", 7)                # días que se mantiene
LST_STOP = _s("LST_STOP", "pico").lower()       # pico = sobre el máximo desde el listado · pct = % fijo · none
LST_STOP_BUF = _f("LST_STOP_BUF", 3.0)          # % por encima del pico
LST_STOP_PCT = _f("LST_STOP_PCT", 35.0)
LST_STOP_MAX = _f("LST_STOP_MAX", 60.0)         # si el pico queda más lejos que esto, no se entra
LST_HEDGE = _b("LST_HEDGE", True)               # cubrir con un largo de BTC del mismo nocional (aísla el efecto listado)
LST_MAX_OPEN = _i("LST_MAX_OPEN", 4)

# ── IDEA 2 · FUNDING CLOCK: antes del cobro, el lado que paga un funding extremo cierra → presión en contra ──
FC_THR = _f("FC_THR", 0.05)                     # |funding| mínimo en % por periodo
FC_WIN_MIN = _i("FC_WIN_MIN", 60)               # minutos antes del cobro en que se entra
FC_MODE = _s("FC_MODE", "pre").lower()          # pre = contra el lado que paga, se cobra el funding · post = rebote tras el cobro
FC_STOP_PCT = _f("FC_STOP_PCT", 1.5)
FC_MAX_OPEN = _i("FC_MAX_OPEN", 4)

# ── IDEA 3 · WEEKEND: perpetuos TradFi 24/7 se mueven con flujo cripto fino el finde y corrigen al abrir el mercado real ──
WK_Z = _f("WK_Z", 1.0)                          # movimiento del finde en desviaciones típicas para operar
WK_ENTRY = _s("WK_ENTRY", "dom21").lower()      # dom21 = domingo 21:00 UTC (antes de abrir CME) · lun14 = lunes 14:00 UTC
WK_HOLD_H = _i("WK_HOLD_H", 24)
WK_STOP_SIG = _f("WK_STOP_SIG", 2.5)            # stop en desviaciones típicas diarias
WK_MAX_OPEN = _i("WK_MAX_OPEN", 4)

# ── investigación ──
RESEARCH_MODULES = _list("RESEARCH_MODULES", "listing,funding,weekend")
RESEARCH_DAYS = _i("RESEARCH_DAYS", 365)
RESEARCH_SYMBOLS = _i("RESEARCH_SYMBOLS", 80)   # para funding clock
RESEARCH_HOLD = _b("RESEARCH_HOLD", True)


def summary():
    return (f"modo {'LIVE' if LIVE else 'SIGNAL'} · ideas {', '.join(MODULES)} · riesgo {RISK_PCT}%/op · "
            f"máx {MAX_OPEN} abiertas · coste {COST_PCT}%/lado")
