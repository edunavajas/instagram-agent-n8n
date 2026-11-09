# Spike: firma de los webhooks de Meta en n8n 2.10.2

Fecha: 2026-11-09. Servidor del episodio (n8n 2.10.2, task runners externos `n8nio/runners:2.10.2`, Coolify).
Se probó con un flujo temporal «ZZ spike firma» publicado en `https://n8n.tudominio.com/webhook/zz-spike-firma`
y peticiones `curl` firmadas con `openssl` desde otra máquina, por HTTPS público. El flujo y sus
ejecuciones se borraron al terminar. Secreto: uno de PRUEBA inventado (no el de Meta).

## 1. Resultado en una frase

Un solo nodo Webhook (GET + POST, misma ruta, `responseMode: responseNode`, `rawBody: true`) atiende la
verificación de Meta y las notificaciones. El GET se resuelve en JavaScript (`src/signature.js`,
`verifyChallenge`). La firma del POST se calcula en el **runner de Python** (`src/signature.py`), que es
el único que puede leer `IG_APP_SECRET` sin abrir el sandbox de JavaScript. Si la firma no cuadra, se
responde 403 y no se ejecuta nada más. Si cuadra, se responde 200 y el flujo sigue.

## 2. Lo probado (peticiones reales por HTTPS)

| Caso | Esperado | Obtenido |
|---|---|---|
| GET `hub.mode=subscribe&hub.challenge=1158201444&hub.verify_token=<bueno>` | 200, `text/plain`, cuerpo `1158201444` | 200 `text/plain; charset=utf-8`, cuerpo `1158201444` (1,0 s en frío; ver gotcha 9) |
| GET con token malo | 403 | 403 (0,07 s) |
| GET sin token | 403 | 403 (0,07 s) |
| POST compacto con acentos, `¡` y emoji, firma buena | 200 | 200 `EVENT_RECEIVED` |
| POST «feo» (espacios, saltos, claves en otro orden, `é` y `🚀` escapados, `ñandú` crudo), firma sobre los bytes crudos | 200 | 200 |
| El mismo POST «feo» firmado sobre `JSON.stringify(JSON.parse(cuerpo))` | 403 | 403 |
| Firma de otro cuerpo | 403 | 403 |
| Sin cabecera `X-Hub-Signature-256` | 403 | 403 |
| Prefijo `sha1=` | 403 | 403 |
| Hex en MAYÚSCULAS | 200 | 200 |
| Secreto del runner distinto al del firmante | 403 | 403 |
| Tras el 200, nodo «Trabajo lento» de 3 s | la respuesta no espera | respuesta en 0,09 s; el nodo terminó 3 s después en la misma ejecución |

Tiempos del POST hasta la respuesta, medidos con `curl` desde fuera (incluye TLS y red, ~70 ms):

| Variante | 1 petición | Ráfaga de 50 en paralelo |
|---|---|---|
| A. Todo en JavaScript (secreto escrito en el nodo; solo para medir) | 0,07-0,11 s | 50×200; total 0,97 s; p50 0,63 s; máx. 0,78 s |
| B. **Elegida**: JS saca el cuerpo crudo + Python verifica con `IG_APP_SECRET` | 0,53-0,59 s (1,4-1,55 s en frío) | 50×200; total 7,7 s; p50 4,1 s; máx. 7,5 s (en frío: máx. 15,4 s) |
| B + nodo JS de 3 s después del 200 | — | 50×200; total 10 s; p50 6,0 s; máx. 9,5 s |
| B, 10 en paralelo | — | 10×200; máx. 1,7 s |

En la variante B el nodo Python tarda ~470 ms por ejecución (el runner de Python arranca un proceso por
tarea) y `N8N_RUNNERS_MAX_CONCURRENCY=5`, así que en ráfaga hacen cola. Ninguna petición falló.
Para una cuenta pequeña es suficiente. Si algún día no lo es, ver §7.

## 3. Nodos para `build_workflows.py` (IG 01)

### 3.1 Webhook (GET + POST en la misma ruta)

```json
{
  "parameters": {
    "multipleMethods": true,
    "httpMethod": ["GET", "POST"],
    "path": "instagram",
    "responseMode": "responseNode",
    "options": { "rawBody": true }
  },
  "name": "Webhook Instagram",
  "type": "n8n-nodes-base.webhook",
  "typeVersion": 2.1,
  "webhookId": "<nid('wh-instagram')>"
}
```

- Con `multipleMethods: true` el nodo tiene **dos salidas en el orden de `httpMethod`**: `main[0]` = GET y `main[1]` = POST (comprobado).
- Sin autenticación: Meta no manda cabeceras propias. La seguridad es la firma.
- `rawBody: true` deja el cuerpo exacto en el binario `data` (`{data: <base64>, mimeType}`) y además el JSON parseado en `json.body`. Las cabeceras llegan en minúsculas: `json.headers['x-hub-signature-256']`.

### 3.2 Rama GET

`Verificar GET` (Code JS, `runOnceForAllItems`). Lleva `src/signature.js` delante (el build quita `module.exports`).
Lee el verify token del nodo `Configuración` **sin ejecutarlo** con `$('Configuración').params` (comprobado: funciona
aunque `Configuración` no esté en la rama del GET; el valor tiene que ser literal, no una expresión):

```js
const cfg = (($('Configuración').params.assignments || {}).assignments || []);
const token = (cfg.find((a) => a.name === 'verify_token') || {}).value;
const r = verifyChallenge($input.first().json.query, token);
return [{ json: { ok: r.ok, reason: r.reason || null, challenge: r.body || '' } }];
```

`GET ok?` (IF v2.2, `{{ $json.ok }}` es true) → `Devolver challenge` / `GET 403`:

```json
{ "type": "n8n-nodes-base.if", "typeVersion": 2.2, "parameters": {
  "conditions": { "options": { "caseSensitive": true, "leftValue": "", "typeValidation": "strict", "version": 2 },
    "conditions": [ { "id": "<uuid>", "leftValue": "={{ $json.ok }}", "rightValue": true,
      "operator": { "type": "boolean", "operation": "true", "singleValue": true } } ],
    "combinator": "and" }, "options": {} } }
```

```json
{ "name": "Devolver challenge", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5,
  "parameters": { "respondWith": "text", "responseBody": "={{ $json.challenge }}",
    "options": { "responseCode": 200,
      "responseHeaders": { "entries": [ { "name": "Content-Type", "value": "text/plain; charset=utf-8" } ] } } } }
```

```json
{ "name": "GET 403", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5,
  "parameters": { "respondWith": "noData", "options": { "responseCode": 403 } } }
```

### 3.3 Rama POST

`Webhook[main 1]` → `Cuerpo crudo` (JS) → `Verificar firma` (Python) → `Firma ok?` (IF, igual que arriba)
→ verdadero: `Responder 200` → resto del flujo (Configuración, `events.js`, dedupe, despacho);
falso: `Responder 403` y **nada conectado detrás**.

`Cuerpo crudo` (Code JS, todos los items). Funciona igual con datos binarios en memoria o en disco:

```js
// Bytes exactos que firmó Meta. No verifica: el secreto solo lo ve el runner de Python.
const item = $input.first();
const raw = await this.helpers.getBinaryDataBuffer(0, 'data');
return [{ json: { raw_b64: raw.toString('base64'), signature: item.json.headers['x-hub-signature-256'] || '' } }];
```

`Verificar firma` (Code Python): `language: "pythonNative"`, `mode: runOnceForAllItems`, `pythonCode` =
contenido de `src/signature.py` + al final:

```python
return [{"json": verify_item(_items[0]["json"])}]
```

Sin `onError: continueErrorOutput`: si el runner de Python falla, la ejecución acaba en error y no se
responde 200 (no probado qué código exacto devuelve n8n; Meta reintenta, que es mejor que aceptar sin verificar). Sin secreto en el runner → `missing_secret` → 403.

```json
{ "name": "Responder 200", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5,
  "parameters": { "respondWith": "text", "responseBody": "EVENT_RECEIVED", "options": { "responseCode": 200 } } }
{ "name": "Responder 403", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5,
  "parameters": { "respondWith": "noData", "options": { "responseCode": 403 } } }
```

El JSON ya parseado para `events.js` está en `$('Webhook Instagram').first().json.body`.

## 4. Configuración del servidor (Coolify, servicio n8n)

Los scripts `ep4-*` que se nombran aquí son del servidor de la serie (repositorio privado de operaciones); no están en este repo.

1. **File Mount** `/etc/n8n-task-runners.json` del contenedor `task-runners` = `runners/n8n-task-runners.json` de este repo.
   Es el del ep3 más tres cosas en el runner **de Python** (nada cambia en el de JavaScript):
   - `IG_APP_SECRET` en `allowed-env`;
   - `base64`, `hashlib` y `hmac` en `N8N_RUNNERS_STDLIB_ALLOW` (queda `base64,email,hashlib,hmac,os,re,smtplib,ssl`);
   - `N8N_BLOCK_RUNNER_ENV_ACCESS=false` ya estaba (lo puso el ep3 para `SMTP_*`).
2. **compose**, servicio `task-runners`, en `environment`: `IG_APP_SECRET=${IG_APP_SECRET}`.
3. **Variable** de Coolify `IG_APP_SECRET`. AHORA tiene un valor de PRUEBA (guardado en `secretos.env` del servidor).
   **En la Fase 4 se sustituye por el App Secret real** (panel de Meta › App settings › Basic › App secret)
   y se reinicia el servicio entero. En el servidor: poner el valor real en `secretos.env` y lanzar
   `ep4-runners-aplicar --yes` (sube la variable, reinicia y comprueba que el contenedor tiene ese valor, sin imprimirlo).
4. Reiniciar el servicio **entero** (n8n + task-runners + n8n-db) para que el runner lea el json y la variable.

`ep4-runners-revertir --yes` deshace 1-3 (vuelve al json del ep3 y borra la variable).

## 5. De dónde sale el App Secret (lo que se investigó)

- `$env.IG_APP_SECRET`: NO. `$env` lo resuelve n8n (no el runner) y la plantilla de Coolify trae `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`.
- Runner de JavaScript: el Code node **no tiene `process`** en su contexto. `N8N_BLOCK_RUNNER_ENV_ACCESS` **no existe** en el
  runner JS de 2.10.2 (solo en el de Python). La única vía es `NODE_FUNCTION_ALLOW_BUILTIN=crypto,process` + `IG_APP_SECRET` en el
  `allowed-env` del runner JS y `require('process').env.IG_APP_SECRET`. Descartada: `process` da también `process.binding`
  (comprobado: `process.binding('spawn_sync')` está disponible en el Node 24.13.1 del runner), es decir, ejecutar comandos
  desde cualquier Code node JS, además de `process.exit()`/`kill`. No se probó en n8n.
- Runner de Python: `os.environ` funciona porque el ep3 ya puso `N8N_BLOCK_RUNNER_ENV_ACCESS=false`. Solo ve las variables
  de su `allowed-env` (comprobado por nombres: `IG_APP_SECRET`, `SMTP_*`, `PATH`, `TZ` y las `N8N_RUNNERS_*` del launcher).
- `require('crypto')` en el runner JS: funciona (`NODE_FUNCTION_ALLOW_BUILTIN=crypto` del ep3), con `require('crypto')` normal.
  Se usa en `verifyChallenge` y en las pruebas; en el flujo final la firma la hace Python.

## 6. Gotchas (lo que falló o sorprendió a la primera)

1. **`JSON.stringify(body)` pasa con cuerpos compactos.** Con un cuerpo compacto en UTF-8 crudo, la firma sobre el JSON
   re-serializado coincidía (los bytes salen iguales). Solo falla con espacios, otro orden de claves o escapes `é`.
   Una implementación mal hecha pasaría las pruebas con `curl` y fallaría con Meta (que, según cuentan muchos desarrolladores,
   escapa los no ASCII como `\uXXXX` y `/` como `\/`; A PROBAR con un webhook real). Por eso las pruebas usan un cuerpo «feo».
2. **El runner JS no puede leer variables de entorno** sin abrir `process` (ver §5). La opción «override de
   `N8N_BLOCK_RUNNER_ENV_ACCESS` en el runner JS» no hace nada en 2.10.2.
3. **Python es lento por tarea**: ~470 ms el nodo, 1,4-1,5 s la primera petición tras 15 s sin uso. En ráfaga de 50, hasta 7,5 s
   (15 s en frío). JavaScript: 1 ms el nodo, 0,78 s la ráfaga entera.
4. **«Security violations detected»** en el primer intento con Python: el sandbox rechaza importar módulos que no estén en
   `N8N_RUNNERS_STDLIB_ALLOW` (`hmac`, `hashlib`) y usar `type()`, `getattr()` o `__name__`. El mensaje no dice qué línea.
5. **Dos salidas del Webhook**: con `multipleMethods` el orden de salidas es el de `httpMethod`. Si se escribe `["POST","GET"]`, se cruzan.
6. **Binario en memoria**: en este servidor el cuerpo crudo llega inline (`binary.data.data` en base64, sin `id`), así que
   también lo ve Python. No se usa: con datos binarios en disco solo funcionaría `this.helpers.getBinaryDataBuffer` (JS).
7. **Coolify no deja editar un File Mount con contenido**: hay que quitar el volumen, borrar el File Mount y volver a
   crearlo con `content:` en el compose (lo hacen `ep3-restaurar` y `ep4-runners-aplicar`).
8. **`N8N_RUNNERS_AUTH_TOKEN` estaba escrito tal cual en el compose** (en n8n y en task-runners). Se rotó y ahora los dos lo
   leen de la variable de Coolify `${N8N_RUNNERS_AUTH_TOKEN}`. Si no coinciden, los runners no se emparejan y los Code nodes fallan.
9. **Arranque en frío de los runners** (`N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT=15`): la primera petición tras 15 s quietos tarda
   ~1 s más (GET y POST).
10. **El cuerpo crudo queda en las ejecuciones** (`raw_b64` y el binario): el texto de los DM se guarda como cualquier dato de
    ejecución. Lo cubre la retención de ejecuciones de n8n, no la purga de 90 días de `ig_*`.
11. El 403 de `noData` sale con `Content-Type: application/json` y cuerpo vacío. A Meta le da igual.

## 7. Abierto

- Sin probar con Meta de verdad (Fase 4): forma exacta del cuerpo, si llega `X-Hub-Signature` (SHA-1) y el plazo de respuesta.
- Si las ráfagas reales hacen cola: subir `N8N_RUNNERS_MAX_CONCURRENCY` del runner Python (no probado) o pasar a la variante A
  aceptando `process` en el runner JS.
- Riesgo que ya venía del ep3: con `os` permitido, un Code node Python puede ejecutar comandos en el contenedor de runners
  (`os.system('true')` devolvió 0). Quien edite flujos puede leer `SMTP_PASSWORD` e `IG_APP_SECRET`. Es el mismo modelo de
  confianza que el ep3: solo Edu edita flujos.
