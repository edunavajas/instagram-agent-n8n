# Fallos de la Fase 3 (pruebas sin Meta)

Fecha: 2026-11-09. Servidor del episodio (n8n 2.10.2), flujos `IG 01…IG 08` publicados con `dry_run = true`,
webhooks falsos firmados con `scripts/send-fake-webhook.sh` contra la URL pública. Lo que falló a la primera:

| # | Síntoma | Causa | Arreglo |
|---|---|---|---|
| 1 | Todos los DM de prueba iban a llegar como `vencido_ventana`. | `test-data/*.json` lleva fechas fijas de noviembre de 2025; un DM de hace más de 24 h llega con la ventana cerrada. | `send-fake-webhook.sh` cambia las fechas por «ahora» (o por `IG_TS_MS`). Caso nuevo `dm-vieja` (25 h) para probar justo eso. |
| 2 | La ráfaga de 50 habría dejado 49 comentarios en `duplicado_usuario`. | Los 50 venían del mismo usuario y en la misma publicación: solo se manda una privada por usuario y publicación. | Cada comentario de la ráfaga viene de un usuario distinto. La ráfaga muestra también p50 y máximo de la respuesta. |
| 3 | «¿A qué hora abrís?» no salió sola: fue a aprobación con el motivo `no_cubre` (confianza 0,90). | No era un fallo del flujo. Ninguna FAQ dice a qué hora abre el taller (solo cuándo se leen los DM, y que sin cita no hay visitas). El modelo contestó con el horario de los DM y el control léxico de la capa 3 (palabras de 5 letras o más: `abris` no sale en las fuentes) lo frenó. | No se tocó ningún umbral. El caso de prueba pregunta algo que la FAQ sí responde: «¿En qué horario contestáis los mensajes?» (sale solo). Limitación anotada: `coversQuestion` no lematiza (`abrís` ≠ `abrimos`) e ignora palabras cortas como `hora`. |
| 4 | El plan decía indexar `kb/faq-instagram.md` con `replace_all: false`. | El flujo `04 - Indexar repositorio` del ep2 lanza un error si no llega `replace_all: true`, y con `true` borra los documentos `repo` que no vengan en el lote. La cabecera de la FAQ, además, hablaba de `replace_all` y ese texto acababa indexado. | `ep4-montar` manda un lote con todos los documentos `repo` que ya hay, sin cambiarlos, más la FAQ nueva. El upsert del ep2 compara `content_hash`, así que la FAQ del ep3 no se toca (mismo hash, misma fecha y el mismo chunk, comprobado antes y después). Cabecera de la FAQ reescrita. |
| 5 | Con `IG 06` sin publicar, `/webhook/ig-button-test` no responde. | En n8n los webhooks de producción solo existen si el flujo está publicado. La nota del flujo decía «NO publiques este flujo en Fase 3». | `IG 06` se publica con el nodo «Telegram» (trigger) desactivado solo en la copia desplegada. `ep4-montar` lo hace solo cuando «Telegram Instagram» lleva el mismo token que el bot del ep3. Nota del flujo corregida. Antes y después de publicar, `getWebhookInfo` confirma que el webhook sigue en el `03b` y que `pending_update_count` sigue a 0. |
| 6 | El resumen decía «Sin enviar · Bloqueados (1 inyección, 3 sensibles, 5 bucle): 9». | Sumaba motivos que se solapan (un DM sensible y retenido por el freno contaba dos veces) y metía DM que luego se aprobaron y sí se enviaron. | Sección propia «No salieron solos (motivo)», con cada motivo en su línea y sin total (`src/format.js` y su prueba). |

## Detalles menores (no arreglados)

- El perdedor de dos DM simultáneos (freno atómico de `IG 04`) queda con `route = auto` y `status = esperando_aprobacion`. El motivo está en `ig_actions` (`retenido_bucle`). Solo afecta a cómo se lee la fila.
- Los avisos de Telegram no dicen si es DRY_RUN (no llevan 🧪). En pruebas llegan al chat real igual que en producción.
- `ep4-montar` actualiza siempre las 3 credenciales nuevas, porque n8n no deja leer sus valores. No duplica nada (mismo id).
