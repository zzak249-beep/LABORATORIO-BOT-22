"""Investigación de las tres ideas con datos reales de BingX. Manda cada informe por Telegram y un .txt final."""
import logging
import os
import re
import time
from datetime import datetime, timezone

import config as C
from bingx import BingX
import nv_funding as funding_clock
import nv_listing as listing
import nv_weekend as weekend
import nv_carry as carry
from notify import Telegram

log = logging.getLogger("research")
RUN = {"carry": carry.research, "listing": listing.research, "funding": funding_clock.research, "weekend": weekend.research}


def main():
    tg = Telegram(C.TG_TOKEN, C.TG_CHAT)
    bx = BingX(C.API_KEY, C.API_SECRET, C.VST)
    tg.send(f"🔬 NOVA · investigación de {', '.join(C.RESEARCH_MODULES)} · {C.CODE_VERSION}\n"
            "No subas archivos a GitHub mientras corre: Railway reinicia el servicio.")
    reports = []
    for m in C.RESEARCH_MODULES:
        fn = RUN.get(m)
        if not fn:
            continue
        try:
            rep = fn(bx, tg)
        except Exception as e:  # noqa: BLE001 — una idea que falla no tumba las otras
            log.exception(m)
            rep = f"❌ {m}: {type(e).__name__}: {e}"
        tg.send(rep)
        reports.append(rep)
    txt = "\n\n".join(reports) + (
        "\n\nCómo leerlo: t agrupado (por semana, hora de cobro o finde) es el que vale; "
        "✅ sobrevive a Bonferroni; si 70%|30% cambia de signo, no es estable. "
        "Una idea solo pasa a LIVE si su mejor variante tiene ✅ y el 30% final es positivo.")
    os.makedirs(C.DATA_DIR, exist_ok=True)
    out = os.path.join(C.DATA_DIR, f"nova_investigacion_{datetime.now(timezone.utc):%Y%m%d_%H%M}.txt")
    with open(out, "w") as f:
        f.write(re.sub(r"</?(b|code|i)>", "", txt))
    tg.send_file(out)
    log.info("listo: %s", out)
    while C.RESEARCH_HOLD:
        time.sleep(3600)
