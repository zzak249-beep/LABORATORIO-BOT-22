"""Configuración desde variables de entorno (los parsers quitan comillas: lección de Railway)."""
import os

CODE_VERSION = "tsmom-bot 1.0.0 (2026-10-04)"


def _raw(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().strip('"').strip("'").strip()
    return default if v == "" else v


def _s(name, default):
    return str(_raw(name, default))


def _f(name, default):
    try:
        return float(_raw(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name, default):
    try:
        return int(float(_raw(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name, default):
    return str(_raw(name, default)).lower() in ("1", "true", "si", "sí", "yes", "on")


def _list(name, default=""):
    return [x.strip().upper() for x in _s(name, default).split(",") if x.strip()]


# ── Modo ──
MODE = _s("MODE", "SIGNAL").upper()               # SIGNAL = cartera virtual · LIVE = órdenes reales
CONFIRM_LIVE = _s("CONFIRM_LIVE", "NO").upper()   # segundo cerrojo
LIVE = MODE == "LIVE" and CONFIRM_LIVE in ("SI", "SÍ", "YES")
VST = _b("BINGX_VST", False)
API_KEY = _s("BINGX_API_KEY", "")
API_SECRET = _s("BINGX_API_SECRET", "")
TG_TOKEN = _s("TELEGRAM_TOKEN", "") or _s("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = _s("TELEGRAM_CHAT_ID", "")
DATA_DIR = _s("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
LOG_LEVEL = _s("LOG_LEVEL", "INFO")

# ── Universo (todas las monedas de BingX que pasen liquidez) ──
SYMBOLS = _list("SYMBOLS", "")                    # vacío = automático
CATEGORIES = [x.strip().lower() for x in _s("CATEGORIES", "crypto,commodity,index,forex,stock,tradfi").split(",")]
MIN_QUOTE_VOL = _f("MIN_QUOTE_VOL", 5_000_000)    # USDT 24h mínimo (cripto)
MIN_QUOTE_VOL_TRADFI = _f("MIN_QUOTE_VOL_TRADFI", 300_000)
MAX_SYMBOLS = _i("MAX_SYMBOLS", 400)
BLACKLIST = _list("BLACKLIST", "")

# ── Temporalidad ──
TIMEFRAME = _s("TIMEFRAME", "1d").lower()         # 1d (artículos) · 4h también vale: horizontes en días
HISTORY_BARS = _i("HISTORY_BARS", 420)            # velas que se descargan por símbolo
MIN_HISTORY_DAYS = _i("MIN_HISTORY_DAYS", 70)     # una moneda con menos historia no entra
RUN_DELAY_MIN = _i("RUN_DELAY_MIN", 10)           # minutos tras el cierre de vela antes de calcular

# ── [1][2] Señal de tendencia (Moskowitz-Ooi-Pedersen · Bouchaud) ──
H1, H2, H3 = _i("H1", 21), _i("H2", 63), _i("H3", 252)
W1, W2, W3 = _f("W1", 1.0), _f("W2", 1.0), _f("W3", 1.0)
SIGNAL_MODE = _s("SIGNAL_MODE", "capada").lower()  # capada (convexa) | signo (TSMOM puro)
ZCAP = _f("ZCAP", 2.0)
DEAD_ZONE = _f("DEAD_ZONE", 0.15)

# ── [1] Riesgo ──
TARGET_VOL = _f("TARGET_VOL", 40.0)               # % anual por activo (paper: 40 %)
VOL_COM = _f("VOL_COM", 60)                       # días, centro de masa de la vol EWMA

# ── [4] Freno de liquidez (Brunnermeier-Pedersen) ──
USE_LIQ = _b("USE_LIQ", True)
VOL_SHORT, VOL_LONG = _f("VOL_SHORT", 10), _f("VOL_LONG", 120)
LIQ_TH = _f("LIQ_TH", 1.6)
LIQ_FREEZE = _b("LIQ_FREEZE", True)

# ── [3] Carry vía funding (Gorton-Hayashi-Rouwenhorst) ──
USE_CARRY = _b("USE_CARRY", True)
CARRY_W = _f("CARRY_W", 0.25)
FUND_SCALE = _f("FUND_SCALE", 0.03)               # % por periodo de funding que satura el carry
FUND_DAYS = _i("FUND_DAYS", 8)

# ── Cartera ──
MAX_POS = _i("MAX_POS", 10)                       # posiciones simultáneas
MIN_SIGNAL = _f("MIN_SIGNAL", 0.30)               # |señal| para ENTRAR
POS_SCALE = _f("POS_SCALE", 0.0)                  # 0 = 1/√MAX_POS: con activos poco correlados la cartera ronda TARGET_VOL
MAX_W = _f("MAX_W", 0.5)                          # peso máximo por activo (fracción del equity)
GROSS_MAX = _f("GROSS_MAX", 2.0)                  # exposición bruta máxima (suma de |pesos|)
LONG_ONLY = _b("LONG_ONLY", False)

# ── [5] Rotación (Barber-Odean) ──
REBAL_DAYS = _i("REBAL_DAYS", 1)
BAND = _f("BAND", 0.25)
MIN_TRADE = _f("MIN_TRADE", 0.02)                 # fracción del equity
COST_PCT = _f("COST_PCT", 0.08)                   # % por lado (comisión + deslizamiento) en la cartera virtual

# ── LIVE ──
LEVERAGE = _i("LEVERAGE", 3)
MARGIN_MODE = _s("MARGIN_MODE", "ISOLATED").upper()
CAT_STOP_SIGMA = _f("CAT_STOP_SIGMA", 4.0)        # stop de catástrofe: K × vol diaria (0 = sin stop)
MAX_DAILY_DD_PCT = _f("MAX_DAILY_DD_PCT", 8.0)    # caída diaria de la cartera que pausa nuevas entradas
EQUITY_FRACTION = _f("EQUITY_FRACTION", 1.0)      # parte del equity de la cuenta que gestiona ESTE bot

# ── Avisos ──
VERDICT_EVERY = _i("VERDICT_EVERY", 20)           # días entre veredictos estadísticos
SIGNAL_EQUITY = _f("SIGNAL_EQUITY", 1000.0)       # capital de la cartera virtual (SIGNAL)

TF_SECONDS = {"1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}


def tf_seconds(tf=None):
    return TF_SECONDS.get(tf or TIMEFRAME, 86400)


def bars_per_day(tf=None):
    return 86400 / tf_seconds(tf)


def summary():
    return (f"modo {'LIVE' if LIVE else 'SIGNAL'} · TF {TIMEFRAME} · horizontes {H1}/{H2}/{H3}d · señal {SIGNAL_MODE}"
            f" · vol objetivo {TARGET_VOL:.0f}% · máx {MAX_POS} posiciones · carry {'on' if USE_CARRY else 'off'}"
            f" · freno {'on' if USE_LIQ else 'off'} · banda {BAND:.0%} · {'solo largos' if LONG_ONLY else 'largos y cortos'}")
