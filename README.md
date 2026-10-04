# TSMOM-C bot · todas las monedas de BingX

Misma lógica que el indicador **TSMOM-C** de TradingView, aplicada a la vez a todos los perpetuos USDT de BingX (cripto + forex, materias primas, índices y acciones) que pasen el filtro de liquidez.

## Qué hace cada día (00:10 UTC)
1. Escanea el universo y calcula la señal de cada moneda:
   tendencia 1m/3m/12m normalizada y capada (convexa) + carry por funding − freno de liquidez.
2. Monta la cartera: se queda con las `MAX_POS` señales más fuertes (|señal| ≥ `MIN_SIGNAL`),
   con un tamaño inverso a la volatilidad. Una posición sigue abierta mientras su señal no cambie de signo
   ni caiga a la zona muerta.
3. Te manda por Telegram cada **ENTRADA / CIERRE / GIRO / AUMENTA / REDUCE** con precio, peso,
   desglose de la señal y stop de catástrofe, la cartera y el ranking de monedas más alcistas y más bajistas.
4. En **SIGNAL** lleva una cartera virtual (con costes y funding reales). En **LIVE** manda órdenes a mercado y
   coloca un STOP_MARKET de catástrofe a `CAT_STOP_SIGMA` × la volatilidad diaria.

## Comandos de Telegram
`/senales` ranking de todas las monedas · `/cartera` · `/estado` · `/veredicto` · `/pausa` · `/reanudar`

## Railway
- **Servicio del bot:** sube la carpeta a GitHub → New Service → pega `railway.env.txt` en el editor de variables (Raw Editor) → añade un volumen en `/data`.
- **Servicio de investigación (recomendado antes de LIVE):** otro servicio con el mismo repositorio y las variables de `railway.research.env.txt`.
  Descarga unos 3 años de velas diarias y funding de 60 monedas, simula 8 variantes con costes y te manda la tabla y un .txt por Telegram.
- Para pasar a real: `MODE=LIVE` **y** `CONFIRM_LIVE=SI`. Usa una subcuenta: el bot solo toca las posiciones que abrió él.

## Diferencias con el indicador de TradingView
- El indicador se aplica a una sola moneda y entra en cuanto la señal sale de la zona muerta. El bot reparte el riesgo entre varias
  y solo entra si la señal es de las más fuertes (|señal| ≥ 0.30) y queda plaza libre. Por eso puede haber marcas en el gráfico que el bot no opera.
- Usa la vela diaria. Comprueba la moneda en TradingView con el gráfico en 1D.

## Avisos honestos
- TSMOM es una estrategia lenta: gana en tendencias largas y pierde poco a poco en mercados laterales. Con menos de ~250 días el veredicto casi nunca será significativo.
- En la investigación, el universo se elige con el volumen de hoy. Eso es sesgo de supervivencia y favorece a los largos.
- El carry se calcula con el funding (funding positivo = largos amontonados = resta a los largos). Es una aproximación a la *basis* del artículo, no la misma cosa.

`python test_offline.py` ejecuta las pruebas sin red.
