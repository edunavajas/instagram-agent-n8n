"""Flujos del lado Meta: IG 01 - Webhook, IG 02 - Comentarios, IG 02b - Reintentos, IG 08 - Graph API.

build_flows() -> {"01-webhook.json": payload, ...}. Los payload salen de pack().
Formas de nodo copiadas del episodio 3 (Postgres, HTTP Request, IF, Telegram, Set,
Schedule, Webhook interno) y de docs/spike-firma.md (Webhook GET+POST, Respond to
Webhook, Code JS «Cuerpo crudo», Code Python «Verificar firma»).
"""

from wf_common import (
    bool_if,
    code,
    config_node,
    http_json,
    link,
    node,
    nid,
    pack,
    postgres,
    py_code,
    sticky,
    switch_if,
    telegram,
    webhook,
)

CFG = "$('Configuración').first().json"


def n8n_url(path):
    """URL interna de otro flujo: n8n se llama a sí mismo (n8n_url de Configuración)."""
    return "={{ " + CFG + ".n8n_url.replace(/\\/$/, '') + '/webhook/" + path + "' }}"


def code_all(name, x, y, body, files=(), notes=""):
    """Code JS en modo «todos los items» (modo explícito, como en el spike)."""
    item = code(name, x, y, body, files=files, each=False, notes=notes)
    item["parameters"]["mode"] = "runOnceForAllItems"
    return item


def js(name, x, y, body, files=(), notes=""):
    """Code JS por item (como el ep3). Evita que `notes` caiga en `each` por posición."""
    return code(name, x, y, body, files=files, each=True, notes=notes)


def py_all(name, x, y, body, files=(), notes=""):
    """Code Python en modo «todos los items» (modo explícito, como en el spike)."""
    item = py_code(name, x, y, body, files=files, each=False, notes=notes)
    item["parameters"]["mode"] = "runOnceForAllItems"
    return item


def once_call(name, x, y, url, body_expr, notes="", timeout=60000):
    """Llamada interna SIN reintentos automáticos: la respuesta privada de Meta es
    única por comentario, así que no se repite a ciegas. El error sale por la salida 1."""
    item = http_json(name, x, y, url, body_expr, "igkey", notes, timeout=timeout)
    item["retryOnFail"] = False
    item.pop("maxTries", None)
    item.pop("waitBetweenTries", None)
    return item


def respond_text(name, x, y, body, code_=200, headers=None, notes=""):
    options = {"responseCode": code_}
    if headers:
        options["responseHeaders"] = {"entries": [{"name": key, "value": value} for key, value in headers]}
    return node(name, "n8n-nodes-base.respondToWebhook", 1.5, x, y, {"respondWith": "text", "responseBody": body, "options": options}, notes)


def respond_empty(name, x, y, code_, notes=""):
    return node(name, "n8n-nodes-base.respondToWebhook", 1.5, x, y, {"respondWith": "noData", "options": {"responseCode": code_}}, notes)


# ---------------------------------------------------------------------------
# IG 01 - Webhook
# ---------------------------------------------------------------------------
def flow_webhook():
    nodes = [
        sticky(
            "Notas webhook", 80, 40,
            "### IG 01 Webhook\n\n"
            "Único punto público: `/webhook/instagram`. Meta no puede mandar cabeceras propias, así que la seguridad es la firma.\n\n"
            "**GET**: verificación (hub.challenge) con el verify token de `Configuración`.\n"
            "**POST**: el JS saca los bytes crudos, el runner de **Python** calcula el HMAC con `IG_APP_SECRET` (variable de task-runners, no está en n8n) y, si no cuadra, 403 y no se hace nada más.\n\n"
            "Si cuadra: **200 antes de procesar**, se normaliza el lote, se deduplica en `ig_events` y cada evento nuevo va por HTTP interno a IG 02 (comentarios) o IG 03 (mensajes). Sin IA.\n\n"
            "Privacidad: este flujo no guarda las ejecuciones correctas (el cuerpo crudo lleva texto de usuarios).",
            width=620, height=330,
        ),
        node(
            "Webhook Instagram", "n8n-nodes-base.webhook", 2.1, 220, 400,
            {
                "multipleMethods": True,
                "httpMethod": ["GET", "POST"],
                "path": "instagram",
                "responseMode": "responseNode",
                "options": {"rawBody": True},
            },
            "Sin cabecera de autenticación (Meta no puede). Salida 0 = GET, salida 1 = POST (el orden es el de httpMethod). rawBody deja los bytes exactos en el binario `data`.",
            {"webhookId": nid("wh-instagram")},
        ),
        # --- GET: verificación de la suscripción ---
        code_all(
            "Verificar GET", 480, 220,
            """
// El verify token se lee de Configuración sin ejecutarla (valor literal, no expresión).
const cfg = (($('Configuración').params.assignments || {}).assignments || []);
const token = (cfg.find((a) => a.name === 'verify_token') || {}).value;
const r = verifyChallenge($input.first().json.query, token);
return [{ json: { ok: r.ok, reason: r.reason || null, challenge: r.body || '' } }];
""",
            ("signature.js",),
            "Responde el hub.challenge solo si el token coincide.",
        ),
        bool_if("¿GET ok?", 740, 220, "={{ $json.ok }}"),
        respond_text("Devolver challenge", 980, 160, "={{ $json.challenge }}", 200, [("Content-Type", "text/plain; charset=utf-8")]),
        respond_empty("GET 403", 980, 300, 403),
        # --- POST: notificaciones firmadas ---
        code_all(
            "Cuerpo crudo", 480, 560,
            """
// Bytes exactos que firmó Meta. No verifica: el secreto solo lo ve el runner de Python.
const item = $input.first();
const raw = await this.helpers.getBinaryDataBuffer(0, 'data');
return [{ json: { raw_b64: raw.toString('base64'), signature: item.json.headers['x-hub-signature-256'] || '' } }];
""",
            notes="Saca el cuerpo crudo en base64 y la cabecera X-Hub-Signature-256.",
        ),
        py_all(
            "Verificar firma", 700, 560,
            'return [{"json": verify_item(_items[0]["json"])}]',
            files=("signature.py",),
            notes="HMAC-SHA256 del cuerpo crudo con IG_APP_SECRET (os.environ del runner de Python). Sin onError: si el runner falla no se responde 200 y Meta reintenta.",
        ),
        bool_if("¿Firma ok?", 940, 560, "={{ $json.ok }}"),
        respond_text("Responder 200", 1180, 500, "EVENT_RECEIVED", 200, notes="Firma buena: se contesta ya y se sigue trabajando después."),
        respond_empty("Responder 403", 1180, 680, 403, "Firma mala: nada conectado detrás."),
        config_node(1420, 500, ["verify_token", "ig_account_id", "ig_username", "n8n_url", "telegram_chat_id"]),
        code_all(
            "Normalizar", 1660, 500,
            """
const cfg = $('Configuración').first().json;
let body = $('Webhook Instagram').first().json.body;
if (typeof body === 'string') {
  try { body = JSON.parse(body); } catch (error) { body = null; }
}
const result = parseWebhook(body, { igAccountId: cfg.ig_account_id, igUsername: cfg.ig_username });
// Lecturas y reacciones llegan a montones y no aportan nada: ni se registran.
const SILENT = ['read', 'reaction'];
const groups = {};
for (const item of result.dropped) {
  if (SILENT.includes(item.reason)) continue;
  const group = groups[item.reason] || (groups[item.reason] = { reason: item.reason, total: 0, refs: [] });
  group.total += 1;
  if (group.refs.length < 5 && item.ref) group.refs.push(String(item.ref));
}
return [{ json: { events: result.events, dropped: Object.values(groups), dropped_total: result.dropped.length } }];
""",
            ("events.js",),
            "Un lote puede traer varios eventos. Los descartes salen agrupados por motivo (sin texto de usuarios).",
        ),
        postgres(
            "Descartes", 1900, 700,
            """INSERT INTO ig_actions (action, detail)
SELECT 'descartado', jsonb_build_object('reason', d.reason, 'total', d.total, 'refs', d.refs)
FROM jsonb_to_recordset($1::jsonb) AS d(reason text, total integer, refs jsonb)
RETURNING id""",
            "={{ [JSON.stringify($json.dropped)] }}",
            "Un solo INSERT por lote, una fila por motivo. Si falla no frena el despacho (onError continuar).",
            {"onError": "continueRegularOutput"},
        ),
        code_all(
            "Separar eventos", 1900, 420,
            """
return ($input.first().json.events || []).map((event) => ({ json: event }));
""",
            notes="Un item por evento. Si el lote no trae eventos útiles, aquí se acaba.",
        ),
        postgres(
            "Deduplicar", 2140, 420,
            """WITH ins AS (
  INSERT INTO ig_events (event_id, kind)
  VALUES ($1::text, $2::text)
  ON CONFLICT (event_id) DO NOTHING
  RETURNING event_id
)
SELECT (SELECT event_id FROM ins) AS inserted_id, $3::jsonb AS event""",
            "={{ [$json.id, $json.kind, JSON.stringify($json)] }}",
            "Meta reintenta hasta 36 h y los anuncios duplican comentarios. Si ya estaba, inserted_id es null. Devuelve el evento para no depender del emparejado de items.",
        ),
        js(
            "Con evento", 2380, 420,
            """
const event = typeof $json.event === 'string' ? JSON.parse($json.event) : $json.event;
return { json: { event, duplicate: !$json.inserted_id } };
""",
        ),
        bool_if("¿Duplicado?", 2600, 420, "={{ $json.duplicate }}", "Sí: se anota y se acaba. No: se despacha."),
        postgres(
            "Anotar duplicado", 2840, 300,
            """INSERT INTO ig_actions (action, detail)
VALUES ('evento_duplicado', jsonb_build_object('event_id', $1::text, 'kind', $2::text))
RETURNING id""",
            "={{ [$json.event.id, $json.event.kind] }}",
            "Auditoría del reintento de Meta. No vuelve a procesar nada.",
        ),
        switch_if("¿Comentario?", 2840, 520, "={{ $json.event.kind }}", "comment", "comment va a IG 02; dm va a IG 03."),
        http_json(
            "Despachar comentario", 3080, 440, n8n_url("ig-comment"), "={{ JSON.stringify($json.event) }}", "igkey",
            "Llama a IG 02 con el evento normalizado. IG 02 responde al instante.", timeout=20000,
        ),
        http_json(
            "Despachar mensaje", 3080, 620, n8n_url("ig-dm"), "={{ JSON.stringify($json.event) }}", "igkey",
            "Llama a IG 03 con el evento normalizado.", timeout=20000,
        ),
        js(
            "Fallo despacho", 3320, 540,
            """
const info = $('Con evento').item.json.event;
const config = $('Configuración').first().json;
const raw = $json.error;
const detail = String(typeof raw === 'string' ? raw : (raw && (raw.message || raw.description)) || JSON.stringify(raw || 'sin detalle')).slice(0, 400);
const target = info.kind === 'comment' ? 'IG 02 - Comentarios (/webhook/ig-comment)' : 'IG 03 - Mensajes (/webhook/ig-dm)';
const hint = `Meta ya recibió el 200, así que no lo reenvía. ¿Está activo ${target}? ¿Coinciden n8n_url y la credencial Instagram API Key? El evento queda guardado en ig_actions (error_despacho) y su fila de ig_events se borra para poder reenviarlo a mano.`;
const chat = String(config.telegram_chat_id || '').trim();
return { json: {
  event_id: info.id,
  kind: info.kind,
  failure: detail,
  event_json: JSON.stringify(info),
  chat_id: chat,
  notify: chat !== '' && chat !== 'cambia-esto',
  text: failureMessage('no se pudo pasar un evento de Instagram a su flujo', { id: info.id, hint }, detail),
} };
""",
            ("format.js",),
            "El texto de Telegram no lleva el comentario ni el mensaje del usuario.",
        ),
        postgres(
            "Anotar fallo despacho", 3560, 540,
            """WITH del AS (
  DELETE FROM ig_events WHERE event_id = $1::text RETURNING event_id
)
INSERT INTO ig_actions (action, detail)
VALUES ('error_despacho', jsonb_build_object('event_id', $1::text, 'kind', $2::text, 'failure', $3::text, 'event', $4::jsonb))
RETURNING id""",
            "={{ [$json.event_id, $json.kind, $json.failure, $json.event_json] }}",
            "Único sitio donde se guarda el texto del evento: sin él no se podría recuperar. Se purga con el resto de ig_actions (90 días).",
        ),
        bool_if("¿Avisar?", 3800, 540, "={{ $('Fallo despacho').item.json.notify }}", "Solo si telegram_chat_id ya no es «cambia-esto»."),
        telegram(
            "Avisar fallo", 4040, 540,
            "={{ $('Fallo despacho').item.json.chat_id }}",
            "={{ $('Fallo despacho').item.json.text }}",
            "Bot de Instagram (credencial Telegram Instagram).",
            on_error="continueRegularOutput",
        ),
    ]
    c = {}
    link(c, "Webhook Instagram", "Verificar GET", 0)
    link(c, "Webhook Instagram", "Cuerpo crudo", 1)
    link(c, "Verificar GET", "¿GET ok?")
    link(c, "¿GET ok?", "Devolver challenge", 0)
    link(c, "¿GET ok?", "GET 403", 1)
    link(c, "Cuerpo crudo", "Verificar firma")
    link(c, "Verificar firma", "¿Firma ok?")
    link(c, "¿Firma ok?", "Responder 200", 0)
    link(c, "¿Firma ok?", "Responder 403", 1)
    link(c, "Responder 200", "Configuración")
    link(c, "Configuración", "Normalizar")
    link(c, "Normalizar", "Separar eventos")
    link(c, "Normalizar", "Descartes")
    link(c, "Separar eventos", "Deduplicar")
    link(c, "Deduplicar", "Con evento")
    link(c, "Con evento", "¿Duplicado?")
    link(c, "¿Duplicado?", "Anotar duplicado", 0)
    link(c, "¿Duplicado?", "¿Comentario?", 1)
    link(c, "¿Comentario?", "Despachar comentario", 0)
    link(c, "¿Comentario?", "Despachar mensaje", 1)
    link(c, "Despachar comentario", "Fallo despacho", 1)
    link(c, "Despachar mensaje", "Fallo despacho", 1)
    link(c, "Fallo despacho", "Anotar fallo despacho")
    link(c, "Anotar fallo despacho", "¿Avisar?")
    link(c, "¿Avisar?", "Avisar fallo", 0)
    flow = pack("IG 01 - Webhook", nodes, c)
    # El cuerpo crudo lleva texto de usuarios: las ejecuciones correctas no se guardan
    # (las erróneas sí, para poder depurar) ni el progreso intermedio.
    flow["settings"]["saveDataSuccessExecution"] = "none"
    flow["settings"]["saveExecutionProgress"] = False
    return flow


# ---------------------------------------------------------------------------
# IG 02 - Comentarios
# ---------------------------------------------------------------------------
UPDATE_SENT = """WITH upd AS (
  UPDATE ig_comments
  SET status = CASE WHEN EXISTS (
        SELECT 1 FROM ig_comments o
        WHERE o.igsid = ig_comments.igsid AND o.media_id IS NOT DISTINCT FROM ig_comments.media_id
          AND o.id <> ig_comments.id AND o.status IN ('respondido', 'privado_enviado')
      ) THEN 'duplicado_usuario' ELSE $2::text END,
      public_reply_id = NULLIF($3::text, ''),
      private_message_id = NULLIF($4::text, ''),
      attempts = $5::integer,
      next_attempt_at = NULL,
      last_error = NULLIF($6::text, 'null')::jsonb
  WHERE id = $1::uuid AND status = 'procesando'
  RETURNING id, igsid, username, media_id, comment_id
), contact AS (
  INSERT INTO ig_contacts (igsid, username, keyword, media_id, comment_id)
  SELECT igsid, username, $7::text, media_id, comment_id FROM upd WHERE igsid IS NOT NULL
  ON CONFLICT (igsid) DO UPDATE
    SET username = COALESCE(EXCLUDED.username, ig_contacts.username),
        keyword = EXCLUDED.keyword, media_id = EXCLUDED.media_id,
        comment_id = EXCLUDED.comment_id, last_seen = now()
  RETURNING igsid
), act AS (
  INSERT INTO ig_actions (comment_id, action, detail)
  SELECT id, $8::text, $9::jsonb FROM upd
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

UPDATE_FAILED = """WITH prev AS (
  SELECT EXISTS (
    SELECT 1 FROM ig_actions
    WHERE action = 'error_comentario' AND detail->>'kind' = $8::text
      AND created_at > now() - interval '10 minutes'
  ) AS seen
), upd AS (
  UPDATE ig_comments
  SET status = $2::text, attempts = $3::integer,
      next_attempt_at = NULLIF($4::text, '')::timestamptz, last_error = $5::jsonb
  WHERE id = $1::uuid AND status = 'procesando'
  RETURNING id
), act AS (
  INSERT INTO ig_actions (comment_id, action, detail)
  SELECT id, $6::text, $7::jsonb FROM upd
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id, NOT (SELECT seen FROM prev) AS first_of_kind"""


def flow_comments():
    nodes = [
        sticky(
            "Notas comentarios", 80, 40,
            "### IG 02 Comentarios\n\n"
            "Recibe de IG 01 un comentario ya normalizado (o de IG 02b un reintento `{retry:true, comment_id}`). **Sin IA**.\n\n"
            "1. Reglas activas de `ig_keywords` + `matchKeyword` (palabra completa, sin acentos ni emojis). **Sin coincidencia no se escribe nada** en la base (solo queda la ejecución de n8n).\n"
            "2. Con coincidencia: `ig_comments` en `procesando` (comment_id único: idempotente). Una privada por usuario y publicación.\n"
            "3. **Primero la respuesta privada** (la crítica: una sola, dentro de 7 días) vía IG 08; después la pública con variante rotada. Si la pública falla no se bloquea nada: queda anotado.\n"
            "4. Error de límite o temporal: `reintento` con espera creciente (IG 02b) mientras no pasen 7 días ni `max_attempts`. Otro error: `error` + aviso a Telegram (uno por tipo cada 10 min).",
            width=700, height=340,
        ),
        webhook("Webhook comentario", "ig-comment", 220, 400, "POST /webhook/ig-comment (cabecera X-IG-API-Key). Responde al instante y sigue."),
        config_node(460, 400, ["ig_account_id", "max_attempts", "window_days", "telegram_chat_id", "n8n_url"]),
        js(
            "Leer petición", 700, 400,
            """
const config = $('Configuración').first().json;
const body = $json.body || $json;
const retry = body.retry === true;
const id = String(retry ? (body.comment_id || '') : (body.id || ''));
const safeId = /^[A-Za-z0-9_.:-]{1,120}$/.test(id);
if (retry) return { json: { retry: true, comment_id: id, valid: safeId } };
const from = body.from && typeof body.from === 'object' ? body.from : {};
const account = String(body.igAccountId || '');
const configured = String(config.ig_account_id || '');
const ownAccount = !configured || configured === 'cambia-esto' || !account || account === configured;
const text = typeof body.text === 'string' ? body.text : '';
const valid = body.kind === 'comment' && safeId && Boolean(from.id) && text.trim() !== '' && ownAccount;
return { json: { retry: false, comment_id: id, media_id: String(body.mediaId || ''), igsid: String(from.id || ''), username: String(from.username || ''), text, ts: String(body.ts || ''), valid } };
""",
            notes="Valida el evento de IG 01 o la petición de reintento de IG 02b.",
        ),
        bool_if("¿Válida?", 940, 400, "={{ $json.valid }}", "No válida: no se hace nada."),
        bool_if("¿Reintento?", 1160, 400, "={{ $json.retry }}", "Sí: se reutiliza la fila que reclamó IG 02b."),
        # --- comentario nuevo ---
        postgres(
            "Reglas", 1400, 560,
            """SELECT COALESCE(jsonb_agg(to_jsonb(k) ORDER BY k.id), '[]'::jsonb) AS rules
FROM ig_keywords k
WHERE k.active""",
            "",
            "Siempre devuelve una fila, con o sin reglas.",
        ),
        js(
            "Coincidencia", 1640, 560,
            """
const request = $('Leer petición').item.json;
const rules = typeof $json.rules === 'string' ? JSON.parse($json.rules) : ($json.rules || []);
const rule = matchKeyword(request.text, rules, request.media_id);
return { json: { ...request, rule: rule || null, matched: Boolean(rule) } };
""",
            ("keywords.js",),
            "Normaliza y busca la palabra clave (la regla de una publicación concreta gana a la global).",
        ),
        bool_if("¿Coincide?", 1880, 560, "={{ $json.matched }}", "No: no se guarda nada y se acaba."),
        postgres(
            "Reservar", 2120, 560,
            """WITH ins AS (
  INSERT INTO ig_comments (comment_id, media_id, igsid, username, text, keyword_id, status, created_at)
  VALUES ($1::text, NULLIF($2::text, ''), $3::text, NULLIF($4::text, ''), $5::text, $6::bigint, 'procesando',
          LEAST(COALESCE(NULLIF($7::text, '')::timestamptz, now()), now()))
  ON CONFLICT (comment_id) DO NOTHING
  RETURNING id, created_at
)
SELECT (SELECT id::text FROM ins) AS id, (SELECT created_at FROM ins) AS created_at""",
            "={{ [$json.comment_id, $json.media_id, $json.igsid, $json.username, $json.text, $json.rule.id, $json.ts] }}",
            "comment_id es único: si ya estaba, otra ejecución lo lleva y esta se acaba. created_at es la hora del comentario (la ventana de 7 días cuenta desde ahí).",
        ),
        js(
            "Con reserva", 2360, 560,
            """
const base = $('Coincidencia').item.json;
if (!$json.id) return { json: { ...base, reserved: false } };
return { json: { ...base, id: $json.id, attempts: 0, created_at: $json.created_at, reserved: true } };
""",
        ),
        bool_if("¿Reservado?", 2600, 560, "={{ $json.reserved }}", "No: duplicado, se acaba."),
        # --- reintento ---
        postgres(
            "Cargar reintento", 1400, 240,
            """SELECT c.id::text AS id, c.comment_id, c.media_id, c.igsid, c.username, c.text, c.attempts, c.created_at, to_jsonb(k) AS rule
FROM ig_comments c
LEFT JOIN ig_keywords k ON k.id = c.keyword_id
WHERE c.comment_id = $1::text AND c.status = 'procesando'""",
            "={{ [$json.comment_id] }}",
            "La fila la dejó en procesando IG 02b. Si no está en ese estado, no devuelve nada y se acaba.",
        ),
        js(
            "Con reintento", 1640, 240,
            """
const rule = typeof $json.rule === 'string' ? JSON.parse($json.rule) : $json.rule;
return { json: { ...$json, rule: rule && rule.id ? rule : null } };
""",
            notes="Si la palabra clave se borró entre medias, rule queda a null y el envío falla con un error claro.",
        ),
        # --- común ---
        js("Comentario a procesar", 2840, 400, "return { json: $json };", notes="Punto de unión de comentario nuevo y reintento."),
        postgres(
            "Comprobar usuario", 3080, 400,
            """WITH me AS (
  SELECT id, igsid, media_id FROM ig_comments WHERE id = $1::uuid AND status = 'procesando'
), dup AS (
  UPDATE ig_comments c SET status = 'duplicado_usuario', next_attempt_at = NULL
  FROM me
  WHERE c.id = me.id AND me.igsid IS NOT NULL AND EXISTS (
    SELECT 1 FROM ig_comments o
    WHERE o.igsid = me.igsid AND o.media_id IS NOT DISTINCT FROM me.media_id
      AND o.id <> me.id AND o.status IN ('respondido', 'privado_enviado')
  )
  RETURNING c.id
), act AS (
  INSERT INTO ig_actions (comment_id, action, detail)
  SELECT id, 'duplicado_usuario', '{}'::jsonb FROM dup
  RETURNING id
)
SELECT (SELECT id::text FROM dup) AS dup_id, EXISTS (SELECT 1 FROM me) AS claimed""",
            "={{ [$json.id] }}",
            "Una respuesta privada por usuario y publicación. Si ya recibió una, esta fila pasa a duplicado_usuario y no se repite.",
        ),
        bool_if("¿Procede?", 3320, 400, "={{ $json.claimed && !$json.dup_id }}", "No: duplicado de usuario o fila ya tratada."),
        js(
            "Preparar privado", 3560, 400,
            """
const work = $('Comentario a procesar').item.json;
const text = work.rule ? buildPrivateMessage(work.rule) : '';
return { json: { ...work, private_text: text } };
""",
            ("keywords.js",),
            "Mensaje de la regla + enlace, máx. 1000 bytes UTF-8.",
        ),
        once_call(
            "Respuesta privada", 3800, 400, n8n_url("ig-graph"),
            "={{ JSON.stringify({ op: 'private_reply', comment_id: $json.comment_id, text: $json.private_text }) }}",
            "IG 08 → POST /{ig_account_id}/messages con recipient.comment_id. Sin reintentos automáticos.",
        ),
        js(
            "Evaluar privada", 4040, 400,
            """
const work = $('Preparar privado').item.json;
const config = $('Configuración').first().json;
let result = $json;
if (result.ok === undefined) {
  const raw = result.error;
  const message = String(typeof raw === 'string' ? raw : (raw && (raw.message || raw.description)) || 'sin respuesta').slice(0, 300);
  result = { ok: false, retryable: true, kind: 'transient', code: null, subcode: null, message: 'IG 08 no respondió: ' + message };
}
const now = new Date().toISOString();
const attempts = Number(work.attempts || 0) + 1;
if (result.ok === true) {
  return { json: { ...work, sent: true, attempts, private_message_id: String(result.message_id || ''), dry_run: result.dry_run === true } };
}
const failure = {
  kind: result.kind || 'unknown',
  code: result.code === undefined ? null : result.code,
  subcode: result.subcode === undefined ? null : result.subcode,
  message: String(result.message || '').slice(0, 300),
  retryable: result.retryable === true,
};
let outcome = 'error';
let reason = failure.kind;
let next = '';
if (failure.retryable) {
  if (attempts >= Number(config.max_attempts)) reason = 'max_intentos';
  else if (!withinPrivateWindow(work.created_at, now, config.window_days)) reason = 'ventana_7_dias';
  else { outcome = 'reintento'; next = nextAttemptAt(attempts, now); }
}
const detail = { kind: failure.kind, code: failure.code, subcode: failure.subcode, reason, attempts, message: failure.message };
return { json: { ...work, sent: false, outcome, reason, attempts, next_attempt_at: next, failure, action: outcome === 'error' ? 'error_comentario' : 'reintento_programado', detail: JSON.stringify(detail) } };
""",
            ("graph.js",),
            "ok → enviada. retryable → reintento con espera (1, 2, 5, 15, 60, 240 min) si queda ventana e intentos; si no, error. Resto → error.",
        ),
        bool_if("¿Privada ok?", 4280, 400, "={{ $json.sent }}"),
        # --- privada enviada: variante y pública ---
        postgres(
            "Elegir variante", 4520, 280,
            """WITH bumped AS (
  UPDATE ig_keywords SET variant_counter = variant_counter + 1
  WHERE id = $1::bigint
  RETURNING variant_counter - 1 AS used
)
SELECT (SELECT used FROM bumped) AS counter""",
            "={{ [$json.rule.id] }}",
            "Contador atómico: dos comentarios a la vez no reciben la misma variante.",
        ),
        js(
            "Preparar pública", 4760, 280,
            """
const work = $('Evaluar privada').item.json;
return { json: { ...work, public_text: pickVariant(work.rule.public_replies, $json.counter) } };
""",
            ("keywords.js",),
        ),
        once_call(
            "Respuesta pública", 5000, 280, n8n_url("ig-graph"),
            "={{ JSON.stringify({ op: 'public_reply', comment_id: $json.comment_id, text: $json.public_text }) }}",
            "IG 08 → POST /{comment_id}/replies. Si falla no se deshace la privada.",
        ),
        js(
            "Evaluar pública", 5240, 280,
            """
const work = $('Preparar pública').item.json;
let result = $json;
if (result.ok === undefined) {
  const raw = result.error;
  result = { ok: false, kind: 'transient', code: null, subcode: null, message: 'IG 08 no respondió: ' + String(typeof raw === 'string' ? raw : (raw && raw.message) || 'sin respuesta').slice(0, 200) };
}
const publicOk = result.ok === true;
const note = publicOk ? null : { public_reply: { kind: result.kind || 'unknown', code: result.code === undefined ? null : result.code, subcode: result.subcode === undefined ? null : result.subcode, message: String(result.message || '').slice(0, 300) } };
return { json: {
  id: work.id,
  status: publicOk ? 'respondido' : 'privado_enviado',
  public_reply_id: publicOk ? String(result.id || '') : '',
  private_message_id: work.private_message_id,
  attempts: work.attempts,
  last_error: JSON.stringify(note),
  keyword: String(work.rule.keyword || ''),
  action: publicOk ? 'respondido' : 'privado_enviado_sin_publica',
  detail: JSON.stringify({ dry_run: work.dry_run === true, keyword_id: work.rule.id, public_ok: publicOk, private_message_id: work.private_message_id }),
} };
""",
            notes="Pública ok → respondido. Pública fallida → privado_enviado y el fallo queda en last_error y en ig_actions.",
        ),
        postgres(
            "Marcar enviado", 5480, 280, UPDATE_SENT,
            "={{ [$json.id, $json.status, $json.public_reply_id, $json.private_message_id, $json.attempts, $json.last_error, $json.keyword, $json.action, $json.detail] }}",
            "Un solo UPDATE: estado final + contacto (upsert en ig_contacts) + ig_actions. Si en medio se coló otra privada al mismo usuario, queda duplicado_usuario en vez de romper el índice único.",
        ),
        # --- privada fallida ---
        postgres(
            "Marcar fallo", 4520, 540, UPDATE_FAILED,
            "={{ [$json.id, $json.outcome, $json.attempts, $json.next_attempt_at, JSON.stringify($json.failure), $json.action, $json.detail, $json.failure.kind] }}",
            "reintento (con next_attempt_at) o error. first_of_kind: ¿es el primer error de ese tipo en 10 minutos? (para no inundar Telegram).",
        ),
        js(
            "Texto aviso", 4760, 540,
            """
const f = $('Evaluar privada').item.json;
const config = $('Configuración').first().json;
const chat = String(config.telegram_chat_id || '').trim();
const notify = f.outcome === 'error' && $json.first_of_kind === true && Boolean($json.id) && chat !== '' && chat !== 'cambia-esto';
const closed = f.reason === 'ventana_7_dias';
const exhausted = f.reason === 'max_intentos';
const notice = {
  ...f.failure,
  kind: closed ? 'window_closed' : (exhausted ? 'transient' : f.failure.kind),
  message: (closed ? 'Pasaron los 7 días de la ventana. ' : (exhausted ? 'Agotados los intentos (' + f.attempts + '). ' : '')) + f.failure.message,
};
const text = notify ? commentErrorNotice({ id: f.id, comment_id: f.comment_id, username: f.username, igsid: f.igsid, text: f.text, keyword: f.rule && f.rule.keyword, attempts: f.attempts }, notice) : '';
return { json: { notify, chat_id: chat, text } };
""",
            ("format.js",),
            "Solo avisa de errores definitivos y solo el primero de cada tipo en 10 minutos.",
        ),
        bool_if("¿Avisar error?", 5000, 540, "={{ $json.notify }}", "Solo si telegram_chat_id ya no es «cambia-esto»."),
        telegram(
            "Avisar error", 5240, 540,
            "={{ $json.chat_id }}",
            "={{ $json.text }}",
            "Bot de Instagram (credencial Telegram Instagram).",
            on_error="continueRegularOutput",
        ),
    ]
    c = {}
    link(c, "Webhook comentario", "Configuración")
    link(c, "Configuración", "Leer petición")
    link(c, "Leer petición", "¿Válida?")
    link(c, "¿Válida?", "¿Reintento?", 0)
    link(c, "¿Reintento?", "Cargar reintento", 0)
    link(c, "¿Reintento?", "Reglas", 1)
    link(c, "Reglas", "Coincidencia")
    link(c, "Coincidencia", "¿Coincide?")
    link(c, "¿Coincide?", "Reservar", 0)
    link(c, "Reservar", "Con reserva")
    link(c, "Con reserva", "¿Reservado?")
    link(c, "¿Reservado?", "Comentario a procesar", 0)
    link(c, "Cargar reintento", "Con reintento")
    link(c, "Con reintento", "Comentario a procesar")
    link(c, "Comentario a procesar", "Comprobar usuario")
    link(c, "Comprobar usuario", "¿Procede?")
    link(c, "¿Procede?", "Preparar privado", 0)
    link(c, "Preparar privado", "Respuesta privada")
    link(c, "Respuesta privada", "Evaluar privada", 0)
    link(c, "Respuesta privada", "Evaluar privada", 1)
    link(c, "Evaluar privada", "¿Privada ok?")
    link(c, "¿Privada ok?", "Elegir variante", 0)
    link(c, "¿Privada ok?", "Marcar fallo", 1)
    link(c, "Elegir variante", "Preparar pública")
    link(c, "Preparar pública", "Respuesta pública")
    link(c, "Respuesta pública", "Evaluar pública", 0)
    link(c, "Respuesta pública", "Evaluar pública", 1)
    link(c, "Evaluar pública", "Marcar enviado")
    link(c, "Marcar fallo", "Texto aviso")
    link(c, "Texto aviso", "¿Avisar error?")
    link(c, "¿Avisar error?", "Avisar error", 0)
    return pack("IG 02 - Comentarios", nodes, c)


# ---------------------------------------------------------------------------
# IG 02b - Reintentos
# ---------------------------------------------------------------------------
def flow_retries():
    nodes = [
        sticky(
            "Notas reintentos", 80, 40,
            "### IG 02b Reintentos\n\n"
            "Cada 5 minutos (o con el botón):\n"
            "1. **Rescata** filas atascadas en `procesando` más de 15 min (una ejecución de IG 02 que murió): vuelven a `reintento` o, si ya van por `max_attempts`, a `error`.\n"
            "2. **Vence** los `reintento` de más de `window_days` días: pasan a `error` (motivo `ventana_7_dias`). Meta ya no deja la respuesta privada.\n"
            "3. **Reclama** hasta 20 `reintento` con `next_attempt_at` vencido (UPDATE atómico a `procesando`) y llama a IG 02 con `{retry:true, comment_id}`.\n\n"
            "La espera entre intentos la fija IG 02 (1, 2, 5, 15, 60, 240 min).",
            width=640, height=300,
        ),
        node("Cada 5 minutos", "n8n-nodes-base.scheduleTrigger", 1.3, 220, 260, {
            "rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]},
        }, "Cada 5 minutos."),
        node("Reintentar ahora", "n8n-nodes-base.manualTrigger", 1, 220, 460, {}, "Para enseñarlo en cámara sin esperar."),
        config_node(460, 360, ["window_days", "max_attempts", "n8n_url"]),
        postgres(
            "Rescatar", 700, 360,
            """WITH stuck AS (
  UPDATE ig_comments
  SET status = CASE WHEN attempts + 1 >= $1::integer THEN 'error' ELSE 'reintento' END,
      attempts = attempts + 1,
      next_attempt_at = CASE WHEN attempts + 1 >= $1::integer THEN NULL ELSE now() END,
      last_error = jsonb_build_object('reason', CASE WHEN attempts + 1 >= $1::integer THEN 'max_intentos' ELSE 'atascado' END)
  WHERE status = 'procesando' AND updated_at < now() - interval '15 minutes'
  RETURNING id, status
), act AS (
  INSERT INTO ig_actions (comment_id, action, detail)
  SELECT id, 'rescatado', jsonb_build_object('status', status) FROM stuck
  RETURNING id
)
SELECT count(*)::integer AS rescued FROM stuck""",
            "={{ [$json.max_attempts] }}",
            "Una ejecución de IG 02 que murió a medias dejaría la fila en procesando para siempre.",
        ),
        postgres(
            "Vencer", 940, 360,
            """WITH expired AS (
  UPDATE ig_comments
  SET status = 'error', next_attempt_at = NULL,
      last_error = COALESCE(last_error, '{}'::jsonb) || jsonb_build_object('reason', 'ventana_7_dias')
  WHERE status = 'reintento' AND created_at <= now() - ($1::numeric * interval '1 day')
  RETURNING id
), act AS (
  INSERT INTO ig_actions (comment_id, action, detail)
  SELECT id, 'ventana_vencida', jsonb_build_object('reason', 'ventana_7_dias') FROM expired
  RETURNING id
)
SELECT count(*)::integer AS expired FROM expired""",
            "={{ [$('Configuración').first().json.window_days] }}",
            "Pasados los 7 días Meta ya no acepta la respuesta privada.",
        ),
        postgres(
            "Reclamar", 1180, 360,
            """WITH due AS (
  SELECT id FROM ig_comments
  WHERE status = 'reintento' AND next_attempt_at <= now()
    AND created_at > now() - ($1::numeric * interval '1 day')
  ORDER BY next_attempt_at
  LIMIT 20
  FOR UPDATE SKIP LOCKED
), claimed AS (
  UPDATE ig_comments c SET status = 'procesando'
  FROM due
  WHERE c.id = due.id AND c.status = 'reintento'
  RETURNING c.comment_id
)
SELECT COALESCE(jsonb_agg(comment_id), '[]'::jsonb) AS ids FROM claimed""",
            "={{ [$('Configuración').first().json.window_days] }}",
            "UPDATE atómico reintento → procesando con LIMIT 20: dos ejecuciones a la vez no se pisan.",
        ),
        code_all(
            "Separar", 1420, 360,
            """
const raw = $input.first().json.ids;
const ids = typeof raw === 'string' ? JSON.parse(raw) : (raw || []);
return ids.map((id) => ({ json: { comment_id: String(id) } }));
""",
            notes="Un item por comentario reclamado. Sin ninguno, se acaba aquí.",
        ),
        http_json(
            "Reintentar", 1660, 360, n8n_url("ig-comment"),
            "={{ JSON.stringify({ retry: true, comment_id: $json.comment_id }) }}", "igkey",
            "IG 02 reutiliza la fila (misma idempotencia). Responde al instante.", timeout=20000, tries=2,
        ),
        postgres(
            "Devolver a reintento", 1900, 480,
            """UPDATE ig_comments
SET status = 'reintento', next_attempt_at = now() + interval '5 minutes'
WHERE comment_id = $1::text AND status = 'procesando'
RETURNING id""",
            "={{ [$('Separar').item.json.comment_id] }}",
            "Si IG 02 no contesta (flujo apagado, n8n reiniciando), la fila no se queda atascada: vuelve a la cola sin gastar un intento.",
        ),
    ]
    c = {}
    link(c, "Cada 5 minutos", "Configuración")
    link(c, "Reintentar ahora", "Configuración")
    link(c, "Configuración", "Rescatar")
    link(c, "Rescatar", "Vencer")
    link(c, "Vencer", "Reclamar")
    link(c, "Reclamar", "Separar")
    link(c, "Separar", "Reintentar")
    link(c, "Reintentar", "Devolver a reintento", 1)
    return pack("IG 02b - Reintentos", nodes, c)


# ---------------------------------------------------------------------------
# IG 08 - Graph API
# ---------------------------------------------------------------------------
LOG_PARAMS_JS = """
function logParams(log, result) {
  return [
    log.op,
    log.method,
    log.path,
    JSON.stringify(log.body === undefined ? null : log.body),
    log.dry_run ? 'true' : 'false',
    log.ok ? 'true' : 'false',
    log.http_status === null || log.http_status === undefined ? '' : String(log.http_status),
    JSON.stringify(log.error === undefined ? null : log.error),
    JSON.stringify(result),
  ];
}
"""


def meta_call(name, x, y, method, notes):
    """HTTP Request hacia Meta con la credencial «Instagram Token». Lee también el
    cuerpo de los errores (fullResponse + neverError) y no revienta si falla la red."""
    parameters = {
        "method": method,
        "url": "={{ $json.req.url }}",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "options": {
            "timeout": 30000,
            "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}},
        },
    }
    if method == "POST":
        parameters["sendBody"] = True
        parameters["specifyBody"] = "json"
        parameters["jsonBody"] = "={{ JSON.stringify($json.req.body) }}"
    return node(
        name, "n8n-nodes-base.httpRequest", 4.2, x, y, parameters, notes,
        {"onError": "continueRegularOutput"}, "igtoken",
    )


def flow_graph():
    nodes = [
        sticky(
            "Notas Graph", 80, 40,
            "### IG 08 Graph API\n\n"
            "**Único sitio que habla con Meta.** Webhook interno `/webhook/ig-graph` con `{op, ...}`:\n"
            "`public_reply` (comment_id, text) · `private_reply` (comment_id, text) · `send_dm` (igsid, text) · `get_profile` (igsid, solo diagnóstico).\n\n"
            "`dry_run` de **este** nodo `Configuración` manda: mientras no sea exactamente `false`, no se llama a Meta; se apunta en `ig_graph_log` lo que se habría enviado y se devuelve un id falso (`dry_...`).\n"
            "Con `false`: `{graph_base}/{graph_version}/...` con la credencial **Instagram Token**. Los errores de Meta se clasifican (`retryable`, `kind`, `code`, `subcode`).\n\n"
            "Responde con el resultado: `{ok, id, recipient_id, message_id, retryable, kind, code, subcode, message, dry_run}`. El token nunca se guarda.",
            width=700, height=330,
        ),
        webhook(
            "Webhook Graph", "ig-graph", 220, 400,
            "POST /webhook/ig-graph (cabecera X-IG-API-Key). Responde con el resultado del último nodo.",
            response_mode="lastNode",
        ),
        config_node(460, 400, ["dry_run", "graph_base", "graph_version", "ig_account_id", "telegram_chat_id"]),
        js(
            "Preparar", 700, 400,
            """
const config = $('Configuración').first().json;
const body = $json.body || $json;
const op = String(body.op || '');
const args = { commentId: String(body.comment_id || body.commentId || ''), igsid: String(body.igsid || body.recipient_id || ''), text: String(body.text || body.message || ''), fields: String(body.fields || '') };
// Seguro por defecto: solo se llama a Meta si dry_run vale exactamente «false».
const dry = String(config.dry_run).trim().toLowerCase() !== 'false';
const invalid = (message) => ({ json: { valid: false, result: { ok: false, id: null, recipient_id: null, message_id: null, retryable: false, kind: 'bad_request', code: null, subcode: null, message, dry_run: dry } } });
if (!GRAPH_OPS.includes(op)) return invalid('operación desconocida: ' + op.slice(0, 40));
const account = String(config.ig_account_id || '').trim();
if ((op === 'private_reply' || op === 'send_dm') && (!account || account === 'cambia-esto') && !dry) return invalid('falta ig_account_id en la Configuración de IG 08');
let req;
try { req = buildRequest(op, args, config); } catch (error) { return invalid(String(error.message).slice(0, 200)); }
return { json: { valid: true, dry_run: dry, op, args, req } };
""",
            ("graph.js",),
            "Valida la operación y arma la petición (sin token). Texto recortado a 1000 bytes UTF-8 en privada y DM.",
        ),
        bool_if("¿Válida?", 940, 400, "={{ $json.valid }}"),
        bool_if("¿Simulación?", 1180, 400, "={{ $json.dry_run }}", "Sí: no se llama a Meta."),
        switch_if("¿Consulta?", 1420, 520, "={{ $json.req.method }}", "GET", "GET = get_profile; el resto son POST."),
        js(
            "Respuesta simulada", 1420, 280,
            LOG_PARAMS_JS + """
const prep = $json;
const fake = dryRunResponse(prep.op, prep.args);
const result = { ok: true, id: fake.id || null, recipient_id: fake.recipient_id || null, message_id: fake.message_id || null, retryable: false, kind: null, code: null, subcode: null, message: null, dry_run: true };
if (prep.op === 'get_profile') { result.name = fake.name || null; result.username = fake.username || null; }
const log = { op: prep.op, method: prep.req.method, path: prep.req.path, body: prep.req.body, dry_run: true, ok: true, http_status: null, error: null };
return { json: { result, params: logParams(log, result) } };
""",
            ("graph.js",),
            "Id falso y determinista (dry_<hash>): repetir una prueba da el mismo id. El cuerpo SÍ se guarda en ig_graph_log.",
        ),
        meta_call("Llamar a Meta", 1660, 560, "POST", "POST a {graph_base}/{graph_version}/... con Authorization: Bearer (credencial Instagram Token). Un 4xx no es error de n8n: se lee el cuerpo."),
        meta_call("Consultar perfil", 1660, 420, "GET", "GET /{igsid}?fields=name,username. Solo diagnóstico."),
        js(
            "Clasificar respuesta", 1900, 500,
            LOG_PARAMS_JS + """
const prep = $('Preparar').item.json;
const status = Number($json.statusCode || 0);
const body = $json.body;
const success = status >= 200 && status < 300 && body && typeof body === 'object' && !body.error;
let result;
if (success) {
  result = { ok: true, id: body.id ? String(body.id) : null, recipient_id: body.recipient_id ? String(body.recipient_id) : null, message_id: body.message_id ? String(body.message_id) : null, retryable: false, kind: null, code: null, subcode: null, message: null, dry_run: false };
  if (prep.op === 'get_profile') { result.name = body.name || null; result.username = body.username || null; }
} else {
  const raw = $json.error;
  const fallback = { error: { message: String(typeof raw === 'string' ? raw : (raw && (raw.message || raw.description)) || 'sin respuesta de Meta').slice(0, 300) } };
  const failure = classifyError(status, body !== undefined && body !== null && body !== '' ? body : fallback);
  result = { ok: false, id: null, recipient_id: null, message_id: null, retryable: failure.retryable, kind: failure.kind, code: failure.code, subcode: failure.subcode, message: failure.message, dry_run: false };
}
const error = result.ok ? null : { kind: result.kind, code: result.code, subcode: result.subcode, message: result.message, retryable: result.retryable };
const log = { op: prep.op, method: prep.req.method, path: prep.req.path, body: null, dry_run: false, ok: result.ok, http_status: status || null, error };
return { json: { result, params: logParams(log, result) } };
""",
            ("graph.js",),
            "2xx → ok. Si no, classifyError (tabla de errores de Meta) → retryable/kind/code/subcode. Sin cuerpo ni token en el log de llamadas reales.",
        ),
        postgres(
            "Registrar llamada", 2140, 400,
            """WITH ins AS (
  INSERT INTO ig_graph_log (op, method, path, body, dry_run, ok, http_status, error)
  VALUES ($1::text, $2::text, $3::text, NULLIF($4::text, 'null')::jsonb, $5::boolean, $6::boolean,
          NULLIF($7::text, '')::integer, NULLIF($8::text, 'null')::jsonb)
  RETURNING id
)
SELECT $9::jsonb AS result, (SELECT id FROM ins) AS log_id""",
            "={{ $json.params }}",
            "ig_graph_log: todas las llamadas simuladas (con el cuerpo) y las reales (sin cuerpo y sin token). Devuelve el resultado para que la respuesta no dependa del emparejado de items. Si falla el INSERT no se pierde el resultado de Meta.",
            {"onError": "continueRegularOutput"},
        ),
        js(
            "Resultado", 2380, 400,
            """
let result = $json.result;
if (typeof result === 'string') result = JSON.parse(result);
if (!result) {
  // El INSERT en ig_graph_log falló: el resultado de Meta no se puede perder.
  for (const name of ['Clasificar respuesta', 'Respuesta simulada']) {
    try { result = $(name).item.json.result; } catch (error) { result = null; }
    if (result) break;
  }
}
return { json: result || { ok: false, retryable: true, kind: 'transient', code: null, subcode: null, message: 'IG 08 sin resultado', dry_run: null } };
""",
            notes="Último nodo de todas las ramas: es lo que devuelve el webhook.",
        ),
    ]
    c = {}
    link(c, "Webhook Graph", "Configuración")
    link(c, "Configuración", "Preparar")
    link(c, "Preparar", "¿Válida?")
    link(c, "¿Válida?", "¿Simulación?", 0)
    link(c, "¿Válida?", "Resultado", 1)
    link(c, "¿Simulación?", "Respuesta simulada", 0)
    link(c, "¿Simulación?", "¿Consulta?", 1)
    link(c, "¿Consulta?", "Consultar perfil", 0)
    link(c, "¿Consulta?", "Llamar a Meta", 1)
    link(c, "Respuesta simulada", "Registrar llamada")
    link(c, "Consultar perfil", "Clasificar respuesta")
    link(c, "Llamar a Meta", "Clasificar respuesta")
    link(c, "Clasificar respuesta", "Registrar llamada")
    link(c, "Registrar llamada", "Resultado")
    return pack("IG 08 - Graph API", nodes, c)


def build_flows():
    return {
        "01-webhook.json": flow_webhook(),
        "02-comentarios.json": flow_comments(),
        "02b-reintentos.json": flow_retries(),
        "08-graph.json": flow_graph(),
    }
