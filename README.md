# NOVA bot · tres ideas que casi nadie opera

Ninguna sale de un patrón de velas. Las tres se apoyan en un **flujo forzado** de dinero que se puede prever.

| Idea | Qué hace | Por qué podría funcionar |
|---|---|---|
| **1 · LISTING** | Abre corto en las monedas recién listadas a las 24 h del listado y lo mantiene 7 días. El stop va sobre el máximo desde el listado. Lo cubre con un largo de BTC del mismo tamaño | Los primeros días venden los airdrops, los fondos con tokens desbloqueados y los market makers, y casi no hay compradores naturales |
| **2 · FUNDING** | Si el funding es extremo (≥0.05 %), entra 60 min antes del cobro en contra del lado que paga y sale pasado el cobro. Además cobra el funding | Quien paga un funding extremo cierra justo antes del cobro. Es un flujo con hora conocida de antemano |
| **3 · WEEKEND** | En los perpetuos TradFi 24/7 (oro, cobre, índices…): si el movimiento de viernes 21:00 a domingo 21:00 UTC supera 1σ, opera en contra durante 24 h | El fin de semana el mercado real está cerrado y el precio lo mueve solo flujo cripto escaso. Al reabrir CME o la bolsa, se corrige |

## Orden de uso
1. **Investigación primero.** Crea un servicio de Railway con `railway.research.env.txt` y un volumen en `/data`. No subas nada a GitHub mientras corre.
   - Te llega por Telegram una tabla por idea: variantes, t agrupado (el que vale), Bonferroni y el 70 %|30 %.
2. **Bot en papel.** Crea otro servicio con `railway.env.txt` (`MODE=SIGNAL`).
   - En `MODULES` pon solo las ideas que salieron con ✅ y el 30 % final positivo.
   - Ajusta sus variables a la mejor variante.
3. **Operar en real.** Solo cuando `/estado` muestre t≥2 con ≥30 operaciones: `MODE=LIVE` + `CONFIRM_LIVE=SI`, mejor en una subcuenta.

## Telegram
- **Entrada:** 🟢/🔴 con precio, stop, hora de cierre programada, tamaño, riesgo y motivo.
- **Cierre:** ✅/❌ con el % neto (comisiones y funding incluidos) y el resultado en R.
- **Resumen diario** a las 20:00 UTC: veredicto por idea.
- **Comandos:** `/estado` · `/abiertas` · `/pausa` · `/reanudar`

## Límites conocidos
- **LISTING:** la API no devuelve las monedas ya deslistadas, que son justo las que más cayeron. El resultado medido sale *peor* de lo real para el corto (es conservador).
- **FUNDING:** BingX solo da ~95 días de velas de 15m. Es una muestra corta, aunque con muchos cobros.
- **WEEKEND:** hay pocos símbolos 24/7 y un solo dato por fin de semana. Necesita meses para concluir algo.
- En LIVE la cobertura de BTC de LISTING abre un largo de BTC real. Si otro bot opera BTC en la misma cuenta, usa una subcuenta.

`python test_offline.py` ejecuta las pruebas sin red.
