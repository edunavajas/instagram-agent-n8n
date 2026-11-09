"""Flujos del lado de MENSAJES DIRECTOS (episodio 4): IG 03, 04, 05, 06 y 07.

build_flows() -> {"03-mensajes.json": …, "04-enviar.json": …, "05-aprobacion.json": …,
                  "06-botones.json": …, "07-resumen.json": …}

Estilo del episodio 3 (email-agent-n8n/scripts/build_workflows.py): estado en
Postgres con UPDATE … WHERE status = '<anterior>' (sin nodos Wait), llamadas
internas por HTTP con la credencial «Instagram API Key», Telegram con la
credencial «Telegram Instagram». El SQL vive en constantes SQL_* para que
scripts/checks/sql_dm.py lo pruebe contra un Postgres de verdad.

Los parámetros de los nodos Postgres se pasan con queryReplacement: n8n
(pg-promise) los formatea en el cliente como literales escapados. El texto del
usuario NUNCA se concatena en el SQL ni en el prompt de sistema.
"""

from wf_common import (
    UUID_RE,
    bool_if,
    code,
    config_node,
    http_json,
    link,
    nid,
    node,
    pack,
    postgres,
    sticky,
    switch_if,
    telegram,
    telegram_edit,
    webhook,
)

CFG = "$('Configuración').first().json"
# telegram_chat_id = «cambia-esto» (o vacío) → no se manda nada a Telegram.
TG_READY = f"={{{{ /^-?\\d{{1,20}}$/.test(String({CFG}.telegram_chat_id || '').trim()) }}}}"


def tg_ready(name, x, y, notes="Sin chat configurado (cambia-esto) no se manda nada a Telegram: queda el rastro en ig_actions."):
    return bool_if(name, x, y, TG_READY, notes)


def call(name, x, y, path, body_expr, notes="", tries=3):
    """POST a otro flujo (n8n se llama a sí mismo) con la cabecera X-IG-API-Key."""
    item = http_json(
        name, x, y,
        f"={{{{ {CFG}.n8n_url.replace(/\\/$/, '') + '/webhook/{path}' }}}}",
        body_expr, "igkey", notes, tries=tries,
    )
    if tries <= 1:
        # Sin reintento automático: repetir la llamada podría mandar el DM dos veces.
        for key in ("retryOnFail", "maxTries", "waitBetweenTries"):
            item.pop(key, None)
    return item


def http_error_js(var="raw"):
    return f"String(typeof {var} === 'string' ? {var} : ({var} && ({var}.message || {var}.description)) || JSON.stringify({var} || 'sin detalle')).slice(0, 400)"


def request_guard(fields, extra_valid="true"):
    """Lee {message_id, …} del webhook y valida el UUID antes de tocar la base."""
    return f"""
const body = $json.body && typeof $json.body === 'object' ? $json.body : $json;
const messageId = String(body.message_id || '').trim().toLowerCase();
const valid = {UUID_RE}.test(messageId) && ({extra_valid});
return {{ json: {{ message_id: messageId, {fields}, valid }} }};
"""


def iso(expr):
    return f"to_char(({expr}) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')"


def window_end(alias):
    """Ventana efectiva (ADENDA §7.5): la del propio DM o la del último DM del mismo IGSID."""
    return f"GREATEST({alias}.window_expires_at, (SELECT max(o.window_expires_at) FROM ig_messages o WHERE o.igsid = {alias}.igsid))"


def message_context(alias):
    """Para rehacer el mensaje con botones de una fila: si la fila apunta a un
    aviso de fallo de envío (con su error) y si pasó por el freno anti-bucles."""
    return f"""EXISTS (SELECT 1 FROM ig_actions a WHERE a.message_id = {alias}.id AND a.action = 'aviso_error_envio'
               AND a.detail->>'telegram_message_id' = {alias}.telegram_message_id) AS on_failure_message,
       (SELECT a.detail->>'error' FROM ig_actions a WHERE a.message_id = {alias}.id AND a.action = 'error_envio'
         ORDER BY a.id DESC LIMIT 1) AS last_send_error,
       (EXISTS (SELECT 1 FROM ig_actions a WHERE a.message_id = {alias}.id AND a.action = 'retenido_bucle')
         OR COALESCE({alias}.reasons @> '["bucle"]'::jsonb, false)) AS loop_hold"""


ROW_COLUMNS = "igsid, username, text_normalized, category, confidence, draft_reply, final_reply, reasoning, reasons, rag_sources, injection_flag, sensitive_flag, telegram_chat_id, telegram_message_id"


def cols(alias):
    return ", ".join(f"{alias}.{name.strip()}" for name in ROW_COLUMNS.split(","))


IGNORED_REQUEST = """INSERT INTO ig_actions (message_id, action, detail)
SELECT id, 'peticion_ignorada', jsonb_build_object('flujo', $2::text, 'estado', status)
FROM ig_messages WHERE id = $1::uuid
RETURNING id"""

# ---------------------------------------------------------------------------
# SQL de IG 03 - Mensajes
# ---------------------------------------------------------------------------

# $1 mid · $2 igsid · $3 username · $4 texto saneado · $5 estado inicial
# (procesando | vencido_ventana | ignorado) · $6 inyección · $7 fin de ventana (ISO o '')
# $8 recibido (ISO o '') · $9 ruta ('' | ignore) · $10 reasons (JSON) · $11 detalle (JSON)
SQL_03_RESERVE = """WITH ins AS (
  INSERT INTO ig_messages
    (mid, igsid, username, text_normalized, status, route, reasons, injection_flag, window_expires_at, received_at)
  VALUES ($1, $2, NULLIF($3::text, ''), $4, $5::text, NULLIF($9::text, ''), $10::jsonb, $6::boolean,
          NULLIF($7::text, '')::timestamptz, NULLIF($8::text, '')::timestamptz)
  ON CONFLICT (mid) DO NOTHING
  RETURNING id, status, injection_flag
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, CASE status WHEN 'vencido_ventana' THEN 'recibido_fuera_de_ventana'
                         WHEN 'ignorado' THEN 'ignorado' ELSE 'recibido' END, $11::jsonb
  FROM ins
  UNION ALL
  SELECT id, 'inyeccion_detectada', $11::jsonb FROM ins WHERE injection_flag
  RETURNING id
)
SELECT (SELECT id::text FROM ins) AS id, (SELECT status FROM ins) AS status"""

SQL_03_DUPLICATE = """INSERT INTO ig_actions (message_id, action, detail)
SELECT id, 'duplicado', jsonb_build_object('mid', $1::text)
FROM ig_messages WHERE mid = $1
RETURNING id"""

# Última respuesta automática a este IGSID (o reservada ahora mismo por IG 04).
SQL_03_LAST_AUTO = f"""SELECT COALESCE({iso("GREATEST((SELECT max(auto_replied_at) FROM ig_messages WHERE igsid = $1 AND status = 'respondido_auto'), (SELECT last_at FROM ig_auto_gate WHERE igsid = $1))")}, '') AS last_auto_at"""

SQL_03_DATES = """SELECT COALESCE(string_agg(id::text || '|' || to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD'), ','), '') AS pairs
FROM knowledge_documents
WHERE id = ANY(string_to_array($1, ',')::uuid[])"""

# $1 id · $2 categoría · $3 confianza · $4 borrador · $5 razonamiento · $6 reasons (JSON)
# $7 fuentes (JSON) · $8 inyección · $9 sensible · $10 ruta · $11-$13 tokens in/out y ms ('' = nulo)
# $14 detalle (JSON)
SQL_03_SAVE = """WITH upd AS (
  UPDATE ig_messages
  SET category = NULLIF($2::text, ''), confidence = $3::numeric, draft_reply = $4, reasoning = $5,
      reasons = $6::jsonb, rag_sources = $7::jsonb, injection_flag = $8::boolean, sensitive_flag = $9::boolean,
      route = $10::text,
      llm_tokens_in = NULLIF($11::text, '')::int, llm_tokens_out = NULLIF($12::text, '')::int,
      llm_ms = NULLIF($13::text, '')::int,
      status = CASE WHEN $10::text <> 'ignore' THEN 'pendiente'
                    WHEN $6::jsonb @> '["ventana_cerrada"]'::jsonb THEN 'vencido_ventana'
                    ELSE 'ignorado' END
  WHERE id = $1::uuid AND status = 'procesando'
  RETURNING id, status
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, 'decidido', $14::jsonb FROM upd
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id, (SELECT status FROM upd) AS status"""

SQL_03_INTERNAL_ERROR = """WITH upd AS (
  UPDATE ig_messages SET status = 'error_interno'
  WHERE id = $1::uuid AND status = 'pendiente'
  RETURNING id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  VALUES ($1::uuid, 'error_interno', jsonb_build_object('destino', $2::text, 'error', $3::text))
  RETURNING id
)
SELECT (SELECT count(*) FROM upd)::int AS updated"""

# ---------------------------------------------------------------------------
# SQL de IG 04 - Enviar
# ---------------------------------------------------------------------------

# $1 id · $2 modo (auto | manual) · $3 loop_minutes
# Una sola sentencia: comprueba la ventana efectiva, reserva el freno anti-bucles
# (ig_auto_gate, solo en auto) y pasa la fila a enviando | retenido_bucle | vencido_ventana.
SQL_04_CLAIM = f"""WITH target AS (
  SELECT m.id, m.igsid, m.status AS previous_status, {window_end("m")} AS window_end
  FROM ig_messages m
  WHERE m.id = $1::uuid
    AND (($2::text = 'auto' AND m.status = 'pendiente' AND m.route = 'auto' AND NOT m.injection_flag)
      OR ($2::text = 'manual' AND m.status = 'aprobado'))
), open AS (
  SELECT t.id, t.igsid FROM target t WHERE t.window_end IS NOT NULL AND t.window_end > now()
), gate AS (
  INSERT INTO ig_auto_gate AS g (igsid, message_id, last_at)
  SELECT o.igsid, o.id, now() FROM open o
  WHERE $2::text = 'auto'
    AND NOT EXISTS (SELECT 1 FROM ig_messages r
                    WHERE r.igsid = o.igsid AND r.id <> o.id AND r.status = 'respondido_auto'
                      AND r.auto_replied_at > now() - make_interval(mins => GREATEST($3::int, 1)))
  ON CONFLICT (igsid) DO UPDATE SET message_id = EXCLUDED.message_id, last_at = EXCLUDED.last_at
  WHERE g.last_at <= now() - make_interval(mins => GREATEST($3::int, 1))
  RETURNING g.igsid
), claimed AS (
  UPDATE ig_messages m
  SET status = CASE
        WHEN NOT EXISTS (SELECT 1 FROM open) THEN 'vencido_ventana'
        WHEN $2::text = 'auto' AND NOT EXISTS (SELECT 1 FROM gate) THEN 'retenido_bucle'
        ELSE 'enviando' END
  FROM target t
  WHERE m.id = t.id AND m.status = t.previous_status
  RETURNING m.id, m.status AS outcome, t.previous_status, t.window_end, {cols("m")}
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT c.id, c.outcome, jsonb_build_object('modo', $2::text, 'flujo', 'IG 04', 'ventana', c.window_end)
  FROM claimed c WHERE c.outcome IN ('vencido_ventana', 'retenido_bucle')
  RETURNING id
)
SELECT c.id::text AS id, c.outcome, c.previous_status, c.window_end AS window_expires_at,
       {cols("c")},
       {message_context("c")}
FROM (SELECT 1) AS one LEFT JOIN claimed c ON true"""

# $1 id · $2 modo · $3 texto enviado · $4 id del mensaje en Instagram · $5 dry_run
SQL_04_MARK_SENT = """WITH upd AS (
  UPDATE ig_messages
  SET status = CASE WHEN $2::text = 'auto' THEN 'respondido_auto' ELSE 'respondido_manual' END,
      final_reply = $3,
      reply_message_id = NULLIF($4::text, ''),
      auto_replied_at = CASE WHEN $2::text = 'auto' THEN now() ELSE auto_replied_at END
  WHERE id = $1::uuid AND status = 'enviando'
  RETURNING id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  VALUES ($1::uuid, CASE WHEN $2::text = 'auto' THEN 'enviado_auto' ELSE 'enviado_manual' END,
          jsonb_build_object('reply_message_id', $4::text, 'dry_run', $5::boolean))
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

# Meta dijo que la ventana está cerrada (10/2534022). $1 id · $2 error (JSON)
SQL_04_MARK_WINDOW = """WITH upd AS (
  UPDATE ig_messages SET status = 'vencido_ventana'
  WHERE id = $1::uuid AND status = 'enviando'
  RETURNING id, igsid
), gate AS (
  DELETE FROM ig_auto_gate g USING upd u WHERE g.igsid = u.igsid AND g.message_id = u.id
  RETURNING g.igsid
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  VALUES ($1::uuid, 'vencido_ventana', $2::jsonb)
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

# $1 id · $2 error (texto) · $3 approval_hours · $4 detalle (JSON)
SQL_04_MARK_ERROR = f"""WITH upd AS (
  UPDATE ig_messages m
  SET status = 'error_envio',
      approval_due_at = LEAST(now() + make_interval(hours => GREATEST($3::int, 1)), {window_end("m")})
  WHERE m.id = $1::uuid AND m.status = 'enviando'
  RETURNING m.id, m.igsid
), gate AS (
  DELETE FROM ig_auto_gate g USING upd u WHERE g.igsid = u.igsid AND g.message_id = u.id
  RETURNING g.igsid
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  VALUES ($1::uuid, 'error_envio', $4::jsonb || jsonb_build_object('error', $2::text))
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

# Los botones del aviso de fallo apuntan a esta fila. $1 id · $2 chat · $3 message_id
SQL_04_SAVE_NOTICE = """WITH upd AS (
  UPDATE ig_messages SET telegram_chat_id = $2, telegram_message_id = $3
  WHERE id = $1::uuid AND status = 'error_envio' AND $3::text <> ''
  RETURNING id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, 'aviso_error_envio', jsonb_build_object('telegram_message_id', $3::text) FROM upd
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

SQL_04_HOLD_FAILED = """INSERT INTO ig_actions (message_id, action, detail)
VALUES ($1::uuid, 'error_llamada_05', jsonb_build_object('error', $2::text))
RETURNING id"""

# ---------------------------------------------------------------------------
# SQL de IG 05 - Pedir aprobación
# ---------------------------------------------------------------------------

# $1 id · $2 motivo ('' | bucle)
SQL_05_CLAIM = f"""WITH target AS (
  SELECT m.id, m.status AS previous_status, {window_end("m")} AS window_end
  FROM ig_messages m
  WHERE m.id = $1::uuid
    AND ((m.status = 'pendiente' AND m.route = 'approval')
      OR (m.status = 'retenido_bucle' AND $2::text = 'bucle'))
), claimed AS (
  UPDATE ig_messages m
  SET status = CASE WHEN t.window_end IS NOT NULL AND t.window_end > now() THEN 'avisando' ELSE 'vencido_ventana' END
  FROM target t
  WHERE m.id = t.id AND m.status = t.previous_status
  RETURNING m.id, m.status AS outcome, t.previous_status, t.window_end, {cols("m")}
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, 'vencido_ventana', jsonb_build_object('flujo', 'IG 05') FROM claimed WHERE outcome = 'vencido_ventana'
  RETURNING id
)
SELECT c.id::text AS id, c.outcome, c.previous_status, c.window_end AS window_expires_at, {cols("c")},
       COALESCE(c.previous_status = 'retenido_bucle' OR c.reasons @> '["bucle"]'::jsonb, false) AS loop_hold
FROM (SELECT 1) AS one LEFT JOIN claimed c ON true"""

# $1 id · $2 chat ('' sin Telegram) · $3 message_id ('' sin Telegram) · $4 approval_hours · $5 acción
SQL_05_SAVE = f"""WITH upd AS (
  UPDATE ig_messages m
  SET status = 'esperando_aprobacion',
      telegram_chat_id = NULLIF($2::text, ''),
      telegram_message_id = NULLIF($3::text, ''),
      approval_due_at = LEAST(now() + make_interval(hours => GREATEST($4::int, 1)), {window_end("m")})
  WHERE m.id = $1::uuid AND m.status = 'avisando'
  RETURNING m.id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, $5::text, jsonb_build_object('telegram_message_id', $3::text) FROM upd
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

SQL_05_FAIL = """WITH upd AS (
  UPDATE ig_messages SET status = 'error_aviso'
  WHERE id = $1::uuid AND status = 'avisando'
  RETURNING id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  VALUES ($1::uuid, 'error_aviso', jsonb_build_object('error', $2::text))
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

# ---------------------------------------------------------------------------
# SQL de IG 06 - Botones
# ---------------------------------------------------------------------------

# $1 id (del callback_data). Siempre devuelve una fila (vacía si no existe).
SQL_06_READ = f"""SELECT m.id::text AS id, m.status, {cols("m")},
       (m.approval_due_at IS NOT NULL AND m.approval_due_at <= now()) AS expired,
       {window_end("m")} AS window_expires_at,
       {message_context("m")}
FROM (SELECT 1) AS one LEFT JOIN ig_messages m ON m.id = $1::uuid"""

# $1 id · $2 estado de antes · $3 estado nuevo · $4 botón
SQL_06_APPLY = """WITH upd AS (
  UPDATE ig_messages m
  SET status = $3::text,
      telegram_edit_id = CASE WHEN $3::text = 'esperando_edicion' THEN NULL ELSE m.telegram_edit_id END
  WHERE m.id = $1::uuid AND m.status = $2::text
    AND $3::text IN ('aprobado', 'rechazado', 'esperando_edicion', 'aprobacion_vencida')
    AND ($3::text = 'aprobacion_vencida' OR m.approval_due_at IS NULL OR m.approval_due_at > now())
    AND ($3::text <> 'aprobado' OR COALESCE(m.final_reply, '') <> ''
         OR (NOT m.injection_flag AND COALESCE(m.draft_reply, '') <> ''))
  RETURNING m.id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, 'boton', jsonb_build_object('boton', $4::text, 'de', $2::text, 'a', $3::text,
                                         'aplicado', (SELECT count(*) FROM upd) > 0)
  FROM ig_messages WHERE id = $1::uuid
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

SQL_06_EDIT_PROMPT = """UPDATE ig_messages SET telegram_edit_id = $2
WHERE id = $1::uuid AND status = 'esperando_edicion' AND $2::text <> ''
RETURNING id::text"""

# Las ediciones abiertas. Solo hay un chat autorizado: son todas suyas.
SQL_06_PENDING = f"""SELECT COALESCE(json_agg(json_build_object(
         'id', id::text, 'telegram_edit_id', telegram_edit_id, 'approval_due_at', {iso("approval_due_at")})
         ORDER BY updated_at DESC), '[]'::json) AS pending
FROM ig_messages
WHERE status = 'esperando_edicion'"""

# $1 id · $2 texto · $3 bytes
SQL_06_SAVE_TEXT = """WITH upd AS (
  UPDATE ig_messages SET final_reply = $2, status = 'aprobado'
  WHERE id = $1::uuid AND status = 'esperando_edicion'
    AND (approval_due_at IS NULL OR approval_due_at > now())
  RETURNING id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, 'editado', jsonb_build_object('bytes', $3::int) FROM upd
  RETURNING id
)
SELECT (SELECT id::text FROM upd) AS id"""

# IG 04 no recibió la petición: la fila vuelve a esperar. $1 id · $2 error
SQL_06_UNDO = f"""WITH upd AS (
  UPDATE ig_messages SET status = 'esperando_aprobacion'
  WHERE id = $1::uuid AND status = 'aprobado'
  RETURNING id, {ROW_COLUMNS}, window_expires_at
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, 'error_llamada_04', jsonb_build_object('error', $2::text) FROM ig_messages WHERE id = $1::uuid
  RETURNING id
)
SELECT u.id::text AS id, {cols("u")}, {window_end("u")} AS window_expires_at,
       {message_context("u")},
       $2::text AS fallo
FROM (SELECT 1) AS one LEFT JOIN upd u ON true"""

SQL_06_FOREIGN = """INSERT INTO ig_actions (message_id, action, detail)
VALUES (NULL, 'telegram_ajeno', jsonb_build_object('tipo', $1::text, 'chat_id', $2::text, 'from_id', $3::text, 'motivo', $4::text))
RETURNING id"""

# ---------------------------------------------------------------------------
# SQL de IG 07 - Resumen
# ---------------------------------------------------------------------------

# Primero la ventana (es el motivo real); luego las aprobaciones que quedan.
SQL_07_EXPIRE = f"""WITH win AS (
  UPDATE ig_messages m SET status = 'vencido_ventana'
  WHERE m.status IN ('pendiente', 'retenido_bucle', 'error_envio', 'esperando_aprobacion', 'esperando_edicion')
    AND {window_end("m")} <= now()
  RETURNING m.id
), appr AS (
  UPDATE ig_messages m SET status = 'aprobacion_vencida'
  WHERE m.status IN ('esperando_aprobacion', 'esperando_edicion') AND m.approval_due_at < now()
    AND m.id NOT IN (SELECT id FROM win)
  RETURNING m.id
), log AS (
  INSERT INTO ig_actions (message_id, action, detail)
  SELECT id, 'vencido_ventana', '{{"flujo": "IG 07"}}'::jsonb FROM win
  UNION ALL
  SELECT id, 'vencido', '{{}}'::jsonb FROM appr
  RETURNING id
)
SELECT (SELECT count(*) FROM win)::int AS expired_windows_now, (SELECT count(*) FROM appr)::int AS expired_approvals_now"""

# $1 retention_days. ig_events a 14 días. Los ig_actions de un DM o comentario se van en cascada.
SQL_07_PURGE = """WITH msgs AS (
  DELETE FROM ig_messages WHERE created_at < now() - make_interval(days => GREATEST($1::int, 1)) RETURNING 1
), comments AS (
  DELETE FROM ig_comments WHERE created_at < now() - make_interval(days => GREATEST($1::int, 1)) RETURNING 1
), contacts AS (
  DELETE FROM ig_contacts WHERE last_seen < now() - make_interval(days => GREATEST($1::int, 1)) RETURNING 1
), orphans AS (
  DELETE FROM ig_actions
  WHERE message_id IS NULL AND comment_id IS NULL AND created_at < now() - make_interval(days => GREATEST($1::int, 1))
  RETURNING 1
), graph AS (
  DELETE FROM ig_graph_log WHERE created_at < now() - make_interval(days => GREATEST($1::int, 1)) RETURNING 1
), gate AS (
  DELETE FROM ig_auto_gate WHERE last_at < now() - make_interval(days => GREATEST($1::int, 1)) RETURNING 1
), events AS (
  DELETE FROM ig_events WHERE received_at < now() - interval '14 days' RETURNING 1
)
SELECT ((SELECT count(*) FROM msgs) + (SELECT count(*) FROM comments) + (SELECT count(*) FROM contacts))::int AS purged,
       (SELECT count(*) FROM msgs)::int AS purged_messages,
       (SELECT count(*) FROM comments)::int AS purged_comments,
       (SELECT count(*) FROM contacts)::int AS purged_contacts,
       (SELECT count(*) FROM orphans)::int AS purged_log,
       (SELECT count(*) FROM graph)::int AS purged_graph_log,
       (SELECT count(*) FROM events)::int AS purged_events,
       (SELECT count(*) FROM gate)::int AS purged_gate"""

# Una sola consulta. Claves = las de summaryMessage (src/format.js).
SQL_07_COUNT = """SELECT c.keyword_comments, c.private_sent,
       m.auto_replied, m.approved, m.rejected, m.expired_approvals, m.expired_windows,
       m.blocked_injection, m.blocked_sensitive, (m.blocked_loop_03 + a.blocked_loop_04)::int AS blocked_loop,
       (m.errors + c.comment_errors)::int AS errors, m.waiting, (m.stuck + c.comment_stuck)::int AS stuck
FROM (
  SELECT
    count(*) FILTER (WHERE status = 'respondido_auto' AND auto_replied_at > now() - interval '1 day')::int AS auto_replied,
    count(*) FILTER (WHERE status = 'respondido_manual' AND updated_at > now() - interval '1 day')::int AS approved,
    count(*) FILTER (WHERE status = 'rechazado' AND updated_at > now() - interval '1 day')::int AS rejected,
    count(*) FILTER (WHERE status = 'aprobacion_vencida' AND updated_at > now() - interval '1 day')::int AS expired_approvals,
    count(*) FILTER (WHERE status = 'vencido_ventana' AND updated_at > now() - interval '1 day')::int AS expired_windows,
    count(*) FILTER (WHERE injection_flag AND created_at > now() - interval '1 day')::int AS blocked_injection,
    count(*) FILTER (WHERE sensitive_flag AND NOT injection_flag AND created_at > now() - interval '1 day')::int AS blocked_sensitive,
    count(*) FILTER (WHERE reasons @> '["bucle"]'::jsonb AND created_at > now() - interval '1 day')::int AS blocked_loop_03,
    count(*) FILTER (WHERE status IN ('error_interno', 'error_envio', 'error_aviso'))::int AS errors,
    count(*) FILTER (WHERE status IN ('esperando_aprobacion', 'esperando_edicion'))::int AS waiting,
    count(*) FILTER (WHERE status IN ('procesando', 'pendiente', 'enviando', 'avisando', 'aprobado', 'retenido_bucle')
                     AND updated_at < now() - interval '1 hour')::int AS stuck
  FROM ig_messages
) m
CROSS JOIN (
  SELECT
    count(*) FILTER (WHERE status <> 'sin_coincidencia' AND created_at > now() - interval '1 day')::int AS keyword_comments,
    count(*) FILTER (WHERE status IN ('respondido', 'privado_enviado') AND updated_at > now() - interval '1 day')::int AS private_sent,
    count(*) FILTER (WHERE status = 'error' AND updated_at > now() - interval '1 day')::int AS comment_errors,
    count(*) FILTER (WHERE status = 'procesando' AND updated_at < now() - interval '1 hour')::int AS comment_stuck
  FROM ig_comments
) c
CROSS JOIN (
  SELECT count(*)::int AS blocked_loop_04
  FROM ig_actions WHERE action = 'retenido_bucle' AND created_at > now() - interval '1 day'
) a"""

SQL_07_NO_TELEGRAM = """INSERT INTO ig_actions (message_id, action, detail)
VALUES (NULL, 'resumen_sin_telegram', $1::jsonb)
RETURNING id"""


# ===========================================================================
# IG 03 - Mensajes
# ===========================================================================

def flow_messages():
    nodes = [
        sticky("Notas mensajes", 80, 40, "### IG 03 Mensajes\n\nLlega desde IG 01 (`POST /webhook/ig-dm`, cabecera X-IG-API-Key) con el evento ya normalizado: `{kind:'dm', id (mid), igAccountId, from:{id}, text, ts}`. Responde al momento.\n\n1. **Inspeccionar**: limpia el texto (capa 1 anti-inyección) y calcula la ventana de 24 h desde `ts`.\n2. **Reservar**: `INSERT … ON CONFLICT (mid) DO NOTHING`. Si ya estaba, es un reintento de Meta: no se llama al modelo.\n3. Ventana ya cerrada al llegar (Meta reintenta hasta 36 h) → `vencido_ventana`, sin Telegram (sale en el resumen). Sin texto → `ignorado`.\n4. RAG (`rag_source_type`), modelo (JSON; DeepSeek sin thinking) y **Decidir** (capa 3, umbral, temas sensibles, freno anti-bucles con la última respuesta automática a ese IGSID).\n5. **Guardar** (tokens y ms del modelo incluidos) → `auto` a IG 04, `approval` a IG 05, `ignore` se queda aquí.\n\nEl texto del usuario va como parámetro en el SQL y como DATO en el mensaje de usuario del modelo, nunca en el prompt de sistema.\n\nSi IG 04 o IG 05 no reciben la petición: `error_interno` y aviso a Telegram.", 640, 420),
        webhook("Webhook DM", "ig-dm", 220, 300, "POST /webhook/ig-dm desde IG 01. Responde 200 al momento."),
        config_node(460, 300, ["confidence_threshold", "min_rag_score", "sensitive_topics", "llm_base_url", "llm_model", "rag_url", "rag_source_type", "loop_minutes", "window_hours", "n8n_url", "telegram_chat_id", "prompt"]),
        code("Inspeccionar", 700, 300, """
const config = $('Configuración').first().json;
const body = $json.body && typeof $json.body === 'object' ? $json.body : $json;
const event = body && typeof body === 'object' ? body : {};
const idOf = (value) => (typeof value === 'string' ? value.trim() : (typeof value === 'number' && Number.isFinite(value) ? String(value) : ''));
const mid = idOf(event.id);
const igsid = event.from && typeof event.from === 'object' ? idOf(event.from.id) : '';
const username = event.from && typeof event.from.username === 'string' ? event.from.username.trim().replace(/^@/, '').slice(0, 80) : '';
const valid = event.kind === 'dm' && mid !== '' && mid.length <= 300 && /^[0-9A-Za-z_-]{1,64}$/.test(igsid);
const now = new Date().toISOString();
const inspection = inspectMessage(typeof event.text === 'string' ? event.text : '', {});
// Sin ts no se sabe cuándo escribió: la ventana cuenta como cerrada (decide.js).
const windowEnd = windowExpiry(event.ts, config.window_hours);
const receivedMs = toMs(event.ts);
// Sin modelo: solo resuelve lo que no lo necesita (ventana cerrada o sin texto).
const pre = decide({ llm: null, inspection, sources: [], cfg: config, now, window: { expiresAt: windowEnd }, lastAutoAt: null });
const early = pre.route === 'ignore';
const status = !early ? 'procesando' : (pre.reasons.includes('ventana_cerrada') ? 'vencido_ventana' : 'ignorado');
return { json: {
  valid, mid, igsid, username,
  text: inspection.sanitized,
  inspection: { sanitized: inspection.sanitized, injection: inspection.injection, patterns: inspection.patterns, ignore: inspection.ignore, reason: inspection.reason, truncated: inspection.truncated },
  window_expires_at: windowEnd || '',
  received_at: Number.isFinite(receivedMs) ? new Date(receivedMs).toISOString() : '',
  status,
  route: early ? 'ignore' : '',
  reasons: early ? pre.reasons : [],
  detail: { patrones: inspection.patterns, inyeccion: inspection.injection, recortado: inspection.truncated, motivos: early ? pre.reasons : [] },
} };
""", ("security.js", "decide.js"), notes="Capa 1: quita invisibles, recorta a 1000 caracteres y marca patrones de inyección. No obedece nada: solo marca."),
        bool_if("¿Válido?", 920, 300, "={{ $json.valid }}", "Un evento sin mid, sin IGSID o que no es un DM no se guarda."),
        postgres(
            "Reservar", 1140, 300, SQL_03_RESERVE,
            "={{ [$json.mid, $json.igsid, $json.username, $json.text, $json.status, $json.inspection.injection === true, $json.window_expires_at, $json.received_at, $json.route, JSON.stringify($json.reasons || []), JSON.stringify($json.detail || {})] }}",
            "La clave es el mid. Dos ejecuciones a la vez (o un reintento de Meta): solo una consigue la fila. Con la ventana cerrada o sin texto la fila nace ya resuelta.",
        ),
        code("Con reserva", 1360, 300, """
const message = $('Inspeccionar').item.json;
return { json: { ...message, id: $json.id || null, duplicate: !$json.id, saved_status: $json.status || '' } };
"""),
        bool_if("¿Duplicado?", 1580, 300, "={{ $json.duplicate }}"),
        postgres("Anotar duplicado", 1800, 120, SQL_03_DUPLICATE, "={{ [$json.mid] }}", "Auditoría del segundo intento. No llama al modelo."),
        switch_if("¿Procesar?", 1800, 360, "={{ $json.saved_status }}", "procesando", "No: ventana cerrada (vencido_ventana) o sin texto (ignorado). Ya está guardado y se acaba aquí."),
        postgres("Última auto", 2020, 360, SQL_03_LAST_AUTO, "={{ [$json.igsid] }}", "Última respuesta automática a este IGSID (o la que IG 04 está enviando ahora). Para el freno anti-bucles."),
        code("Con historial", 2240, 360, """
const message = $('Con reserva').item.json;
return { json: { ...message, last_auto_at: $json.last_auto_at || '' } };
"""),
        http_json(
            "Buscar RAG", 2460, 360,
            f"={{{{ {CFG}.rag_url }}}}",
            f"={{{{ JSON.stringify({{ query: String($json.text || '').slice(0, 1200), limit: 5, source_type: {CFG}.rag_source_type || 'repo' }}) }}}}",
            "rag",
            "Misma base del episodio 2 (FAQ Taller Norte + kb/faq-instagram.md). Cabecera X-RAG-API-Key.",
            text=True,
        ),
        code("Unir fuentes", 2680, 360, """
const message = $('Con historial').item.json;
let parsed = [];
const raw = $json.rag_raw || $json.data || '';
if (!$json.error && raw) {
  try { parsed = JSON.parse(raw); } catch (error) { parsed = []; }
}
if (parsed && parsed.error) parsed = [];
if (!Array.isArray(parsed)) parsed = [];
const sources = parsed.slice(0, 5).map((row) => ({
  document_id: row.document_id || null,
  title: String(row.title || '').slice(0, 300),
  canonical_url: String(row.canonical_url || '').slice(0, 500),
  score: Number(row.score || 0),
  content: String(row.content || '').slice(0, 1500),
  source_type: row.source_type || '',
  chunk_index: row.chunk_index,
})).sort((a, b) => b.score - a.score);
const ids = sources.map((row) => row.document_id).filter((id) => /^[0-9a-f-]{36}$/i.test(String(id)));
return { json: { ...message, sources, source_ids: ids.join(',') } };
""", notes="Si el RAG falla, la lista queda vacía y el DM va a una persona."),
        bool_if("¿Hay ids?", 2900, 360, "={{ $json.source_ids !== '' }}"),
        postgres("Fechas", 3120, 240, SQL_03_DATES, "={{ [$json.source_ids] }}", "El webhook del episodio 2 no trae la fecha. Se lee de knowledge_documents."),
        code("Mezclar fechas", 3340, 240, """
const message = $('Unir fuentes').item.json;
const pairs = String($json.pairs || '').split(',').filter(Boolean);
const dates = {};
for (const pair of pairs) {
  const at = pair.indexOf('|');
  if (at > 0) dates[pair.slice(0, at)] = pair.slice(at + 1);
}
const sources = (message.sources || []).map((source) => ({ ...source, updated_at: dates[source.document_id] || null }));
return { json: { ...message, sources } };
"""),
        code("Preparar bloque", 3560, 360, """
const message = $json.sources ? $json : $('Unir fuentes').item.json;
const userBlock = buildUserBlock({ text: message.text, sources: message.sources || [], now: new Date() });
return { json: { ...message, user_block: userBlock, llm_started_at: Date.now() } };
""", ("prompt.js",), notes="Capa 2: el DM y las fuentes van como DATOS entre marcas, en el mensaje de usuario. El prompt de sistema es fijo (Configuración)."),
        http_json(
            "Clasificar", 3780, 360,
            f"={{{{ {CFG}.llm_base_url.replace(/\\/$/, '') + '/chat/completions' }}}}",
            f"={{{{ JSON.stringify(Object.assign({{ model: {CFG}.llm_model, temperature: 0, max_tokens: 1200, response_format: {{ type: 'json_object' }}, messages: [ {{ role: 'system', content: {CFG}.prompt }}, {{ role: 'user', content: $json.user_block }} ] }}, /api\\.deepseek\\.com/.test({CFG}.llm_base_url) ? {{ thinking: {{ type: 'disabled' }} }} : {{}})) }}}}",
            "llm",
            "JSON estricto. Con DeepSeek, thinking desactivado (si no, piensa por defecto y cuesta más). Si falla, el DM va a una persona.",
        ),
        code("Decidir", 4000, 360, """
const base = $('Preparar bloque').item.json;
const config = $('Configuración').first().json;
const failed = Boolean($json.error);
const usage = !failed && $json.usage && typeof $json.usage === 'object' ? $json.usage : {};
const decision = decide({
  llm: failed ? null : $json,
  inspection: base.inspection,
  sources: base.sources || [],
  cfg: config,
  now: new Date().toISOString(),
  window: { expiresAt: base.window_expires_at },
  lastAutoAt: base.last_auto_at || null,
});
const intOrEmpty = (value) => (Number.isFinite(Number(value)) && value !== null && value !== '' ? String(Math.round(Number(value))) : '');
const topScore = (base.sources || []).reduce((max, source) => Math.max(max, Number(source.score) || 0), 0);
return { json: {
  ...base, ...decision,
  llm_tokens_in: intOrEmpty(usage.prompt_tokens),
  llm_tokens_out: intOrEmpty(usage.completion_tokens),
  llm_ms: intOrEmpty(Date.now() - Number(base.llm_started_at)),
  detail: { ruta: decision.route, motivos: decision.reasons, patrones: base.inspection.patterns, json_error: decision.jsonError || '', mejor_fuente: topScore, modelo_fallo: failed },
} };
""", ("decide.js",), notes="Capa 3: valida el JSON y decide. auto solo si TODO pasa: umbral, fuentes, temas sensibles (mensaje + borrador), sin promesas, sin enlaces ajenos, sin fuga del prompt, ≤1000 bytes, ventana abierta y freno anti-bucles."),
        postgres(
            "Guardar", 4220, 360, SQL_03_SAVE,
            "={{ [$json.id, $json.category || '', Number($json.confidence) || 0, $json.draft || '', $json.reasoning || '', JSON.stringify($json.reasons || []), JSON.stringify(($json.sources || []).map((s) => ({ document_id: s.document_id, title: s.title, canonical_url: s.canonical_url, score: s.score, updated_at: s.updated_at || null }))), $json.injection === true, $json.sensitive === true, $json.route, $json.llm_tokens_in, $json.llm_tokens_out, $json.llm_ms, JSON.stringify($json.detail || {})] }}",
            "procesando → pendiente (auto o approval), ignorado o vencido_ventana. Guarda motivos, tokens y ms del modelo.",
        ),
        code("Con id", 4440, 360, """
const decision = $('Decidir').item.json;
if (!$json.id) return { json: { ...decision, skipped: true } };
return { json: { ...decision, id: $json.id, saved_status: $json.status, skipped: false } };
"""),
        bool_if("¿Guardado?", 4660, 360, "={{ !$json.skipped && $json.saved_status === 'pendiente' }}"),
        switch_if("¿Auto?", 4880, 360, "={{ $json.route }}", "auto"),
        switch_if("¿Aprobar?", 5100, 480, "={{ $json.route }}", "approval"),
        call("Pedir envío", 5320, 260, "ig-send", "={{ JSON.stringify({ message_id: $json.id, mode: 'auto' }) }}", "IG 04 lo envía (o lo frena). Aquí no se envía nada."),
        call("Pedir aprobación", 5320, 460, "ig-approval", "={{ JSON.stringify({ message_id: $json.id }) }}", "IG 05 manda el Telegram y se acaba. No hay nodo Wait."),
        code("Fallo interno", 5560, 640, f"""
const message = $('Con id').item.json;
const config = $('Configuración').first().json;
const raw = $json.error;
const error = {http_error_js()};
const target = message.route === 'auto' ? 'IG 04 (ig-send)' : 'IG 05 (ig-approval)';
const hint = '¿Está activo ' + target + '? ¿n8n_url y la credencial Instagram API Key cuadran en los dos flujos? La fila queda en error_interno.';
return {{ json: {{ id: message.id, target, fallo: error, chat_id: String(config.telegram_chat_id), text: failureMessage('no se pudo pasar el DM a ' + target, {{ ...message, hint }}, error) }} }};
""", ("format.js",), notes="Las salidas de error de las dos llamadas llegan aquí. No se falla en silencio."),
        postgres("Marcar error interno", 5780, 640, SQL_03_INTERNAL_ERROR, "={{ [$json.id, $json.target, $json.fallo] }}"),
        tg_ready("¿Telegram? (fallo)", 6000, 640),
        telegram("Avisar fallo", 6220, 640, "={{ $('Fallo interno').item.json.chat_id }}", "={{ $('Fallo interno').item.json.text }}", "Si Telegram también falla, queda el rastro en la base.", on_error="continueRegularOutput"),
    ]
    c = {}
    link(c, "Webhook DM", "Configuración")
    link(c, "Configuración", "Inspeccionar")
    link(c, "Inspeccionar", "¿Válido?")
    link(c, "¿Válido?", "Reservar", 0)
    link(c, "Reservar", "Con reserva")
    link(c, "Con reserva", "¿Duplicado?")
    link(c, "¿Duplicado?", "Anotar duplicado", 0)
    link(c, "¿Duplicado?", "¿Procesar?", 1)
    link(c, "¿Procesar?", "Última auto", 0)
    link(c, "Última auto", "Con historial")
    link(c, "Con historial", "Buscar RAG")
    link(c, "Buscar RAG", "Unir fuentes", 0)
    link(c, "Buscar RAG", "Unir fuentes", 1)
    link(c, "Unir fuentes", "¿Hay ids?")
    link(c, "¿Hay ids?", "Fechas", 0)
    link(c, "¿Hay ids?", "Preparar bloque", 1)
    link(c, "Fechas", "Mezclar fechas")
    link(c, "Mezclar fechas", "Preparar bloque")
    link(c, "Preparar bloque", "Clasificar")
    link(c, "Clasificar", "Decidir", 0)
    link(c, "Clasificar", "Decidir", 1)
    link(c, "Decidir", "Guardar")
    link(c, "Guardar", "Con id")
    link(c, "Con id", "¿Guardado?")
    link(c, "¿Guardado?", "¿Auto?", 0)
    link(c, "¿Auto?", "Pedir envío", 0)
    link(c, "¿Auto?", "¿Aprobar?", 1)
    link(c, "¿Aprobar?", "Pedir aprobación", 0)
    link(c, "Pedir envío", "Fallo interno", 1)
    link(c, "Pedir aprobación", "Fallo interno", 1)
    link(c, "Fallo interno", "Marcar error interno")
    link(c, "Marcar error interno", "¿Telegram? (fallo)")
    link(c, "¿Telegram? (fallo)", "Avisar fallo", 0)
    return pack("IG 03 - Mensajes", nodes, c)


# ===========================================================================
# IG 04 - Enviar
# ===========================================================================

def flow_send():
    prep = "$('Preparar envío').item.json"
    nodes = [
        sticky("Notas enviar", 80, 40, "### IG 04 Enviar\n\n`POST /webhook/ig-send` con `{message_id, mode: 'auto'|'manual'}`. Solo sale si la fila está lista: `pendiente` con ruta auto (desde IG 03) o `aprobado` (desde IG 06).\n\n**Reclamar** es UNA sentencia: comprueba la **ventana efectiva** (la del DM o la del último DM de ese usuario), y en modo auto reserva el **freno anti-bucles** en `ig_auto_gate` (máx. 1 respuesta automática por IGSID cada `loop_minutes`). Dos DM simultáneos del mismo usuario: solo uno sale solo; el otro pasa a `retenido_bucle` y va a IG 05. Un doble toque sobre la misma fila: solo una ejecución gana el UPDATE.\n\nVentana cerrada → `vencido_ventana`, no se envía, aviso a Telegram y el mensaje del botón se marca.\n\nEnvía SIEMPRE a través de IG 08 (`op: send_dm`), el único que habla con Meta (y el que sabe si es DRY_RUN). Texto recortado a 1000 bytes UTF-8. Sin reintento automático de esa llamada: repetirla podría mandar el DM dos veces.\n\nError de Meta de ventana → como ventana cerrada. Otro error → `error_envio` con aviso y botones Reintentar / Editar / Rechazar (como el ep3).", 640, 420),
        webhook("Webhook enviar", "ig-send", 220, 300, "POST /webhook/ig-send desde IG 03 (auto) o IG 06 (manual)."),
        config_node(460, 300, ["window_hours", "loop_minutes", "approval_hours", "telegram_chat_id", "n8n_url"]),
        code("Leer petición", 700, 300, request_guard("mode: ['auto', 'manual'].includes(body.mode) ? body.mode : ''", "['auto', 'manual'].includes(body.mode)")),
        bool_if("¿Válida?", 920, 300, "={{ $json.valid }}"),
        postgres("Reclamar", 1140, 300, SQL_04_CLAIM, f"={{{{ [$json.message_id, $json.mode, {CFG}.loop_minutes] }}}}",
                 "pendiente(auto)/aprobado(manual) → enviando | retenido_bucle | vencido_ventana, en una sola sentencia. Si otra ejecución ya la cogió, no devuelve id."),
        bool_if("¿Reclamada?", 1360, 300, "={{ !!$json.id }}"),
        postgres("Anotar ignorada", 1580, 120, IGNORED_REQUEST, "={{ [$('Leer petición').item.json.message_id, 'IG 04 ' + $('Leer petición').item.json.mode] }}",
                 "Ya enviada, rechazada, vencida o en curso. No se envía otra vez."),
        code("Preparar envío", 1580, 300, """
const request = $('Leer petición').item.json;
const blocked = $json.injection_flag === true;
// Un DM bloqueado solo sale con el texto que escribió una persona (Editar).
const chosen = request.mode === 'manual'
  ? (String($json.final_reply || '').trim() || (blocked ? '' : String($json.draft_reply || '')))
  : (blocked ? '' : String($json.draft_reply || ''));
const text = truncateUtf8(chosen.trim(), GRAPH_DM_MAX_BYTES).trim();
const fromTelegram = request.mode === 'manual' && String($json.telegram_chat_id || '') !== '' && String($json.telegram_message_id || '') !== '';
return { json: {
  ...$json,
  mode: request.mode,
  text,
  bytes: utf8Bytes(text),
  truncated: utf8Bytes(chosen.trim()) > GRAPH_DM_MAX_BYTES,
  edited: String($json.final_reply || '').trim() !== '',
  from_telegram: fromTelegram,
} };
""", ("graph.js",), notes="Texto final, recortado a 1000 bytes UTF-8 sin partir caracteres."),
        code("Textos Telegram", 1800, 300, """
const config = $('Configuración').first().json;
const row = { ...$json, reason_labels: REASONS, loop_minutes: config.loop_minutes };
// El mensaje con botones de esta fila: el de IG 05 o, si apunta a un aviso de fallo, ese aviso.
const base = $json.from_telegram
  ? ($json.on_failure_message && $json.last_send_error ? sendFailureMessage(row, $json.last_send_error) : approvalMessage(row))
  : '';
const when = madridTime(new Date());
return { json: {
  ...$json,
  chat_id: String(config.telegram_chat_id),
  telegram_base: base,
  sent_edit_text: base ? withResult(base, resultLine($json.edited ? 'enviado_editado' : 'enviado', { when })) : '',
  window_edit_text: base ? withResult(base, resultLine('vencido_ventana', { when })) : '',
} };
""", ("format.js", "decide.js"), notes="Textos para editar el mensaje del botón. Se rehacen desde la fila, como en el ep3."),
        switch_if("¿Enviar?", 2020, 300, "={{ $json.outcome }}", "enviando"),
        switch_if("¿Retenido?", 2240, 520, "={{ $json.outcome }}", "retenido_bucle", "No: la ventana ya estaba cerrada (vencido_ventana)."),
        call("Pedir aprobación", 2460, 460, "ig-approval", f"={{{{ JSON.stringify({{ message_id: {prep}.id, reason: 'bucle' }}) }}}}",
             "Freno anti-bucles: IG 05 lo manda a Telegram con el motivo. Aprobar vuelve aquí en modo manual (sin freno)."),
        code("Fallo aprobación", 2680, 460, f"""
const message = $('Textos Telegram').item.json;
const raw = $json.error;
const error = {http_error_js()};
const hint = 'No se envía y sigue en retenido_bucle. ¿Está activo IG 05 (ig-approval)? Cuando esté, pide la aprobación otra vez: POST /webhook/ig-approval con {{"message_id": "<la fila>", "reason": "bucle"}}.';
return {{ json: {{ id: message.id, fallo: error, chat_id: message.chat_id, text: failureMessage('no se pudo pedir aprobación de un DM retenido por el freno anti-bucles', {{ ...message, hint }}, error) }} }};
""", ("format.js",)),
        postgres("Anotar fallo aprobación", 2900, 460, SQL_04_HOLD_FAILED, "={{ [$json.id, $json.fallo] }}"),
        tg_ready("¿Telegram? (retenido)", 3120, 460),
        telegram("Avisar retenido", 3340, 460, "={{ $('Fallo aprobación').item.json.chat_id }}", "={{ $('Fallo aprobación').item.json.text }}",
                 "Sin botones: la fila aún no está en aprobación.", on_error="continueRegularOutput"),
        bool_if("¿Hay texto?", 2240, 220, "={{ $json.text !== '' }}", "Sin texto (borrador vacío o bloqueado sin editar) no se llama a Meta: va como error."),
        call("Llamar a Meta", 2460, 140, "ig-graph",
             "={{ JSON.stringify({ op: 'send_dm', igsid: $json.igsid, text: $json.text, message_id: $json.id, mode: $json.mode }) }}",
             "IG 08 hace la llamada (o la simula en DRY_RUN) y devuelve {ok, message_id, retryable, kind, code, subcode, message, dry_run}.", tries=1),
        code("Resultado", 2680, 220, """
const prep = $('Textos Telegram').item.json;
let res = $json;
if (prep.text === '') {
  res = { ok: false, kind: 'sin_texto', retryable: false, message: 'No hay texto que enviar (borrador vacío o bloqueado sin editar). Usa Editar.' };
} else if ($json.error) {
  // IG 08 no respondió o devolvió 4xx/5xx: se intenta leer su JSON del mensaje de error.
  const raw = $json.error;
  const text = String(typeof raw === 'string' ? raw : (raw && (raw.message || raw.description)) || JSON.stringify(raw || '')).slice(0, 2000);
  let parsed = null;
  const match = text.match(/\\{[\\s\\S]*\\}/);
  if (match) { try { parsed = JSON.parse(match[0]); } catch (error) { parsed = null; } }
  res = parsed && typeof parsed.ok === 'boolean' ? parsed : { ok: false, kind: 'interno', retryable: true, message: 'IG 08 (ig-graph) no respondió: ' + text.slice(0, 300) };
}
if (res && res.body && typeof res.body === 'object' && typeof res.body.ok === 'boolean') res = res.body;
res = res && typeof res === 'object' ? res : {};
const ok = res.ok === true;
return { json: {
  ...prep,
  ok,
  kind: ok ? '' : String(res.kind || 'unknown'),
  retryable: !ok && res.retryable === true,
  code: res.code ?? '',
  subcode: res.subcode ?? '',
  error_message: ok ? '' : String(res.message || 'sin detalle').slice(0, 400),
  reply_message_id: String(res.message_id || res.id || ''),
  dry_run: res.dry_run === true || res.dry_run === 'true',
} };
""", notes="Lee la respuesta de IG 08 sea cual sea la salida."),
        bool_if("¿Enviado?", 2900, 220, "={{ $json.ok }}"),
        postgres("Marcar enviado", 3120, 100, SQL_04_MARK_SENT, "={{ [$json.id, $json.mode, $json.text, $json.reply_message_id, $json.dry_run] }}",
                 "enviando → respondido_auto | respondido_manual, con el id del mensaje en Instagram y auto_replied_at."),
        code("Texto enviado", 3340, 100, """
const sent = $('Resultado').item.json;
return { json: { ...sent, notice: sentNotice({ ...sent, final_reply: sent.text }, { mode: sent.mode, dryRun: sent.dry_run }) } };
""", ("format.js",)),
        bool_if("¿Venía de Telegram?", 3560, 100, "={{ $json.from_telegram === true }}", "Aprobado con un botón: ese mensaje se marca como enviado. Va después de Marcar enviado: el DM ya salió."),
        telegram_edit("Telegram: enviado", 3780, 20, "={{ $('Texto enviado').item.json.telegram_chat_id }}", "={{ $('Texto enviado').item.json.telegram_message_id }}",
                      "={{ $('Texto enviado').item.json.sent_edit_text }}", "«✅ Aprobado y enviado · hh:mm». Si Telegram falla, no pasa nada: el DM ya salió."),
        bool_if("¿Avisar envío?", 3780, 180, f"={{{{ $('Texto enviado').item.json.mode === 'auto' && /^-?\\d{{1,20}}$/.test(String({CFG}.telegram_chat_id || '').trim()) }}}}",
                "Respuesta automática: llega un aviso «🤖 Respondido solo» (con 🧪 si fue DRY_RUN)."),
        telegram("Avisar respondido", 4000, 180, "={{ $('Texto enviado').item.json.chat_id }}", "={{ $('Texto enviado').item.json.notice }}", on_error="continueRegularOutput"),
        switch_if("¿Ventana cerrada en Meta?", 3120, 300, "={{ $json.kind }}", "window_closed", "Meta respondió 10/2534022 o 10/2018278: la ventana de 24 h está cerrada. No se reintenta."),
        postgres("Marcar ventana cerrada", 3340, 300, SQL_04_MARK_WINDOW,
                 "={{ [$json.id, JSON.stringify({ flujo: 'IG 04', kind: $json.kind, code: $json.code, subcode: $json.subcode, error: $json.error_message })] }}"),
        code("Texto ventana", 3560, 400, """
// Llega desde ¿Retenido? (ventana cerrada al reclamar) o desde Marcar ventana cerrada (Meta).
const row = $('Textos Telegram').item.json;
return { json: { ...row, notice: windowClosedNotice({ ...row, final_reply: row.text }) } };
""", ("format.js",), notes="Llega desde Reclamar (ventana ya cerrada) o desde la respuesta de Meta."),
        bool_if("¿Venía de Telegram? (ventana)", 3780, 400, "={{ $json.from_telegram === true }}", "Las dos salidas siguen al aviso."),
        telegram_edit("Telegram: ventana cerrada", 4000, 340, "={{ $('Texto ventana').item.json.telegram_chat_id }}", "={{ $('Texto ventana').item.json.telegram_message_id }}",
                      "={{ $('Texto ventana').item.json.window_edit_text }}", "«⌛ No se envió: la ventana de 24 h ya se cerró», sin botones."),
        tg_ready("¿Telegram? (ventana)", 4220, 400),
        telegram("Avisar ventana", 4440, 400, "={{ $('Texto ventana').item.json.chat_id }}", "={{ $('Texto ventana').item.json.notice }}", on_error="continueRegularOutput"),
        postgres("Marcar error", 3340, 560, SQL_04_MARK_ERROR,
                 f"={{{{ [$json.id, ($json.kind ? '[' + $json.kind + '] ' : '') + $json.error_message, {CFG}.approval_hours, JSON.stringify({{ kind: $json.kind, code: $json.code, subcode: $json.subcode, retryable: $json.retryable, modo: $json.mode }})] }}}}",
                 "enviando → error_envio. Plazo para decidir: approval_hours, nunca más allá de la ventana. Libera el freno anti-bucles."),
        code("Texto fallo", 3560, 560, """
const sent = $('Resultado').item.json;
const error = (sent.kind ? '[' + sent.kind + (sent.code !== '' ? ' ' + sent.code + (sent.subcode !== '' ? '/' + sent.subcode : '') : '') + '] ' : '') + sent.error_message;
const failed = sent.telegram_base ? withResult(sent.telegram_base, resultLine('fallo_envio', { detail: error, when: madridTime(new Date()) })) : '';
return { json: { ...sent, error, notice: sendFailureMessage({ ...sent, reason_labels: REASONS }, error), failed_edit_text: failed } };
""", ("format.js", "decide.js")),
        bool_if("¿Venía de Telegram? (fallo)", 3780, 560, "={{ $json.from_telegram === true }}", "Las dos salidas siguen al aviso con Reintentar."),
        telegram_edit("Telegram: falló", 4000, 500, "={{ $('Texto fallo').item.json.telegram_chat_id }}", "={{ $('Texto fallo').item.json.telegram_message_id }}",
                      "={{ $('Texto fallo').item.json.failed_edit_text }}", "El mensaje que pulsaste se queda sin botones y con «⚠️ Falló el envío». Reintentar llega en el aviso nuevo."),
        tg_ready("¿Telegram? (fallo)", 4220, 560),
        telegram("Avisar fallo", 4440, 560, "={{ $('Texto fallo').item.json.chat_id }}", "={{ $('Texto fallo').item.json.notice }}",
                 "Reintentar = Aprobar en IG 06 (vuelve aquí en modo manual si la ventana sigue abierta).",
                 buttons=[("Reintentar", "={{ 'ig:approve:' + $('Texto fallo').item.json.id }}"), ("Editar", "={{ 'ig:edit:' + $('Texto fallo').item.json.id }}"), ("Rechazar", "={{ 'ig:reject:' + $('Texto fallo').item.json.id }}")],
                 on_error="continueRegularOutput"),
        postgres("Guardar aviso", 4660, 560, SQL_04_SAVE_NOTICE,
                 "={{ [$('Texto fallo').item.json.id, String($('Texto fallo').item.json.chat_id), $json.result && $json.result.message_id ? String($json.result.message_id) : ''] }}",
                 "Los botones del aviso apuntan a esta fila."),
    ]
    c = {}
    link(c, "Webhook enviar", "Configuración")
    link(c, "Configuración", "Leer petición")
    link(c, "Leer petición", "¿Válida?")
    link(c, "¿Válida?", "Reclamar", 0)
    link(c, "Reclamar", "¿Reclamada?")
    link(c, "¿Reclamada?", "Preparar envío", 0)
    link(c, "¿Reclamada?", "Anotar ignorada", 1)
    link(c, "Preparar envío", "Textos Telegram")
    link(c, "Textos Telegram", "¿Enviar?")
    link(c, "¿Enviar?", "¿Hay texto?", 0)
    link(c, "¿Enviar?", "¿Retenido?", 1)
    link(c, "¿Retenido?", "Pedir aprobación", 0)
    link(c, "¿Retenido?", "Texto ventana", 1)
    link(c, "Pedir aprobación", "Fallo aprobación", 1)
    link(c, "Fallo aprobación", "Anotar fallo aprobación")
    link(c, "Anotar fallo aprobación", "¿Telegram? (retenido)")
    link(c, "¿Telegram? (retenido)", "Avisar retenido", 0)
    link(c, "¿Hay texto?", "Llamar a Meta", 0)
    link(c, "¿Hay texto?", "Resultado", 1)
    link(c, "Llamar a Meta", "Resultado", 0)
    link(c, "Llamar a Meta", "Resultado", 1)
    link(c, "Resultado", "¿Enviado?")
    link(c, "¿Enviado?", "Marcar enviado", 0)
    link(c, "¿Enviado?", "¿Ventana cerrada en Meta?", 1)
    link(c, "Marcar enviado", "Texto enviado")
    link(c, "Texto enviado", "¿Venía de Telegram?")
    link(c, "¿Venía de Telegram?", "Telegram: enviado", 0)
    link(c, "¿Venía de Telegram?", "¿Avisar envío?", 1)
    link(c, "¿Avisar envío?", "Avisar respondido", 0)
    link(c, "¿Ventana cerrada en Meta?", "Marcar ventana cerrada", 0)
    link(c, "¿Ventana cerrada en Meta?", "Marcar error", 1)
    link(c, "Marcar ventana cerrada", "Texto ventana")
    link(c, "Texto ventana", "¿Venía de Telegram? (ventana)")
    link(c, "¿Venía de Telegram? (ventana)", "Telegram: ventana cerrada", 0)
    link(c, "¿Venía de Telegram? (ventana)", "¿Telegram? (ventana)", 1)
    link(c, "Telegram: ventana cerrada", "¿Telegram? (ventana)")
    link(c, "¿Telegram? (ventana)", "Avisar ventana", 0)
    link(c, "Marcar error", "Texto fallo")
    link(c, "Texto fallo", "¿Venía de Telegram? (fallo)")
    link(c, "¿Venía de Telegram? (fallo)", "Telegram: falló", 0)
    link(c, "¿Venía de Telegram? (fallo)", "¿Telegram? (fallo)", 1)
    link(c, "Telegram: falló", "¿Telegram? (fallo)")
    link(c, "¿Telegram? (fallo)", "Avisar fallo", 0)
    link(c, "Avisar fallo", "Guardar aviso")
    return pack("IG 04 - Enviar", nodes, c)


# ===========================================================================
# IG 05 - Pedir aprobación
# ===========================================================================

def flow_ask():
    text = "$('Texto Telegram').item.json"
    nodes = [
        sticky("Notas aprobación", 80, 40, "### IG 05 Pedir aprobación\n\n`POST /webhook/ig-approval` con `{message_id}` (desde IG 03) o `{message_id, reason: 'bucle'}` (desde IG 04, freno anti-bucles).\n\nManda a Telegram el DM, el borrador, las fuentes y los motivos, con Aprobar / Editar / Rechazar (`callback_data` `ig:<acción>:<uuid>`). Guarda el message_id y aquí termina: los botones los coge IG 06. Sin nodo Wait.\n\nBorrador bloqueado (inyección, promesa, fuga del prompt, enlace ajeno, fuente sospechosa) o vacío: sin botón Aprobar.\n\nPlazo: `approval_due_at = LEAST(now() + approval_hours, fin de la ventana de 24 h)`. Si la ventana ya se cerró, `vencido_ventana` y no se manda nada.\n\n`telegram_chat_id = cambia-esto` (pruebas sin bot): no se manda nada, la fila queda en `esperando_aprobacion` sin mensaje y se apunta en ig_actions. Los botones se simulan con `/webhook/ig-button-test` (IG 06).", 640, 360),
        webhook("Webhook aprobación", "ig-approval", 220, 300, "POST /webhook/ig-approval desde IG 03 o IG 04."),
        config_node(460, 300, ["approval_hours", "telegram_chat_id", "telegram_user_ids", "n8n_url"]),
        code("Leer id", 700, 300, request_guard("reason: body.reason === 'bucle' ? 'bucle' : ''")),
        bool_if("¿Válida?", 920, 300, "={{ $json.valid }}"),
        postgres("Reclamar", 1140, 300, SQL_05_CLAIM, "={{ [$json.message_id, $json.reason] }}",
                 "pendiente (ruta approval) o retenido_bucle → avisando (o vencido_ventana). Si llega dos veces, la segunda no manda otro Telegram."),
        bool_if("¿Reclamada?", 1360, 300, "={{ !!$json.id }}"),
        postgres("Anotar ignorada", 1580, 120, IGNORED_REQUEST, "={{ [$('Leer id').item.json.message_id, $('Leer id').item.json.reason ? 'IG 05 ' + $('Leer id').item.json.reason : 'IG 05'] }}"),
        switch_if("¿Ventana abierta?", 1580, 300, "={{ $json.outcome }}", "avisando", "No: la ventana de 24 h ya se cerró. Queda en vencido_ventana (sale en el resumen)."),
        code("Texto Telegram", 1800, 300, """
const config = $('Configuración').first().json;
const row = { ...$json, reason_labels: REASONS };
const text = approvalMessage(row, new Date());
const blocked = $json.injection_flag === true || String($json.draft_reply || '').trim() === '';
return { json: { ...$json, blocked, telegram_text: text, chat_id: String(config.telegram_chat_id).trim(), hours: Number(config.approval_hours) || 24 } };
""", ("format.js", "decide.js"), notes="Usuario, DM, categoría, confianza, motivos (REASONS de decide.js), borrador y fuentes. Bloqueado = sin borrador."),
        tg_ready("¿Telegram?", 2020, 300),
        postgres("Guardar sin Telegram", 2240, 480, SQL_05_SAVE, f"={{{{ [{text}.id, '', '', {text}.hours, 'pedido_aprobacion_sin_telegram'] }}}}",
                 "Pruebas sin bot: esperando_aprobacion sin mensaje. Se aprueba con /webhook/ig-button-test."),
        bool_if("¿Bloqueado?", 2240, 300, "={{ $json.blocked }}"),
        telegram("Avisar", 2460, 380, "={{ $json.chat_id }}", "={{ $json.telegram_text }}", "Aprobar, Editar y Rechazar. callback_data ig:<acción>:<uuid> (47 bytes, límite 64).",
                 buttons=[("Aprobar", "={{ 'ig:approve:' + $json.id }}"), ("Editar", "={{ 'ig:edit:' + $json.id }}"), ("Rechazar", "={{ 'ig:reject:' + $json.id }}")]),
        telegram("Avisar sin aprobar", 2460, 200, "={{ $json.chat_id }}", "={{ $json.telegram_text }}", "Borrador bloqueado o vacío: no hay botón Aprobar.",
                 buttons=[("Editar", "={{ 'ig:edit:' + $json.id }}"), ("Rechazar", "={{ 'ig:reject:' + $json.id }}")]),
        code("Id de Telegram", 2680, 300, f"""
const row = {text};
const messageId = $json.result && $json.result.message_id !== undefined ? String($json.result.message_id) : '';
return {{ json: {{ id: row.id, chat_id: row.chat_id, telegram_message_id: messageId, hours: row.hours }} }};
"""),
        postgres("Guardar botón", 2900, 300, SQL_05_SAVE, "={{ [$json.id, $json.chat_id, $json.telegram_message_id, $json.hours, 'pedido_aprobacion'] }}",
                 "avisando → esperando_aprobacion con telegram_chat_id y telegram_message_id."),
        postgres("Fallo aviso", 2680, 520, SQL_05_FAIL,
                 f"={{{{ [{text}.id, String(($json.error && ($json.error.message || $json.error.description)) || $json.error || 'Telegram no respondió').slice(0, 400)] }}}}",
                 "Telegram no aceptó el mensaje (token, chat id). La fila queda en error_aviso y sale en el resumen."),
    ]
    c = {}
    link(c, "Webhook aprobación", "Configuración")
    link(c, "Configuración", "Leer id")
    link(c, "Leer id", "¿Válida?")
    link(c, "¿Válida?", "Reclamar", 0)
    link(c, "Reclamar", "¿Reclamada?")
    link(c, "¿Reclamada?", "¿Ventana abierta?", 0)
    link(c, "¿Reclamada?", "Anotar ignorada", 1)
    link(c, "¿Ventana abierta?", "Texto Telegram", 0)
    link(c, "Texto Telegram", "¿Telegram?")
    link(c, "¿Telegram?", "¿Bloqueado?", 0)
    link(c, "¿Telegram?", "Guardar sin Telegram", 1)
    link(c, "¿Bloqueado?", "Avisar sin aprobar", 0)
    link(c, "¿Bloqueado?", "Avisar", 1)
    link(c, "Avisar", "Id de Telegram", 0)
    link(c, "Avisar", "Fallo aviso", 1)
    link(c, "Avisar sin aprobar", "Id de Telegram", 0)
    link(c, "Avisar sin aprobar", "Fallo aviso", 1)
    link(c, "Id de Telegram", "Guardar botón")
    return pack("IG 05 - Pedir aprobación", nodes, c)


# ===========================================================================
# IG 06 - Botones
# ===========================================================================

def answer_query(name, x, y, query_expr, text_expr, alert_expr, notes=""):
    return node(name, "n8n-nodes-base.telegram", 1.2, x, y, {
        "resource": "callback",
        "operation": "answerQuery",
        "queryId": query_expr,
        "additionalFields": {"text": text_expr, "show_alert": alert_expr},
    }, notes, {"onError": "continueRegularOutput"}, "telegram")


def flow_buttons():
    what = "$('Texto del botón').item.json"
    nodes = [
        sticky("Notas botones", 80, 40, "### IG 06 Botones\n\n**Trigger de Telegram** (bot del episodio 4, credencial «Telegram Instagram») y, para pruebas, `POST /webhook/ig-button-test` (cabecera X-IG-API-Key) con un update de Telegram inventado: el mismo camino.\n\n⚠️ **Sin el bot nuevo (pruebas)**: desactiva el nodo «Telegram» (clic derecho › Deactivate) y publica el flujo. Un bot solo tiene UN webhook: si este trigger usara el bot del ep3, le quitaría el webhook a «03b - Botones» del correo. Desactivado, no registra nada en Telegram; y el flujo tiene que estar publicado porque `/webhook/ig-button-test` (como cualquier webhook de producción) solo existe con el flujo publicado. El `chat.id` del update de prueba tiene que ser el de `telegram_chat_id`. Con el bot nuevo: vuelve a activar el nodo y republica.\n\nCada botón lleva `ig:<acción>:<uuid>`. **Leer fila** → `callbackOutcome` (telegram.js) → **Aplicar botón** (`UPDATE … WHERE status = <el de antes>`). Doble clic o botón viejo: el UPDATE no toca nada, se relee la fila y sale solo el aviso. Nunca se envía dos veces.\n\nAprobar → `aprobado` → IG 04 modo manual. Editar → `esperando_edicion` y un mensaje de ayuda: con UNA edición abierta vale cualquier texto del chat; con varias, hay que responder citando el mensaje de ayuda (`editOutcome`). Rechazar → `rechazado`. Tras decidir, el mensaje pierde los botones.\n\nChats no autorizados: solo `telegram_ajeno` en ig_actions, sin respuesta.", 680, 460),
        node("Telegram", "n8n-nodes-base.telegramTrigger", 1.2, 220, 260, {
            "updates": ["callback_query", "message"],
            "additionalFields": {},
        }, "Bot del ep4. Sin 'Restrict to Chat IDs': en 2.10.2 descarta los botones. El filtro está en Leer update. Sin bot nuevo: desactivado (no publicar este nodo con el bot del ep3).", {"webhookId": nid("wh-telegram-ig06")}, "telegram"),
        webhook("Probar botón", "ig-button-test", 220, 460, "POST /webhook/ig-button-test con un update de Telegram (callback_query o message). Solo pruebas."),
        config_node(460, 360, ["approval_hours", "telegram_chat_id", "telegram_user_ids", "n8n_url"]),
        code("Leer update", 700, 360, """
// El trigger entrega el update tal cual; el webhook de prueba, dentro de body.
const fromWebhook = $json.body !== undefined && $json.headers !== undefined;
const update = fromWebhook ? $json.body : $json;
return { json: { ...parseUpdate(update, $('Configuración').first().json), test: fromWebhook } };
""", ("telegram.js",), notes="Comprueba chat.id (y from.id si hay lista) contra la Configuración. Lee ig:<acción>:<uuid>."),
        switch_if("¿Botón?", 920, 360, "={{ $json.kind }}", "button"),
        postgres("Leer fila", 1140, 200, SQL_06_READ, "={{ [$json.igMessageId] }}", "La fila del botón, por el uuid del callback_data."),
        code("Qué hacer", 1360, 200, """
const update = $('Leer update').item.json;
const row = $json.id ? $json : null;
const outcome = callbackOutcome(row, update.action);
const OPEN_STATES = ['esperando_aprobacion', 'esperando_edicion', 'error_envio'];
let from = '';
let to = '';
if (outcome.apply) { from = outcome.fromStatus; to = outcome.nextStatus; }
else if (row && outcome.result === 'vencido' && OPEN_STATES.includes(String(row.status))) { from = String(row.status); to = 'aprobacion_vencida'; }
return { json: { ...update, row: row || {}, id: row ? row.id : '', ...outcome, db: to !== '', db_from: from, db_to: to } };
""", ("telegram.js",), notes="Matriz estado × botón (telegram.js). Un plazo vencido se apunta como aprobacion_vencida."),
        bool_if("¿Aplicar?", 1580, 200, "={{ $json.db }}"),
        postgres("Aplicar botón", 1800, 120, SQL_06_APPLY, "={{ [$json.id, $json.db_from, $json.db_to, $json.action] }}",
                 "UPDATE … WHERE status = <el de antes>. Doble clic: el segundo no toca nada."),
        bool_if("¿Aplicado?", 2020, 120, "={{ !!$json.id }}"),
        code("Aplicado", 2240, 40, """
return { json: { ...$('Qué hacer').item.json, applied: true } };
"""),
        postgres("Releer fila", 2240, 200, SQL_06_READ, "={{ [$('Qué hacer').item.json.id] }}", "Otra pulsación (u otro flujo) se adelantó: se lee el estado de verdad."),
        code("Sin cambios", 2460, 200, """
const what = $('Qué hacer').item.json;
const row = $json.id ? $json : null;
const again = callbackOutcome(row, what.action);
// Solo aviso: no se aplica nada, aunque el estado nuevo admitiera el botón.
const notice = again.apply ? 'Otra pulsación se adelantó. Mira el mensaje y vuelve a pulsar si hace falta.' : again.notice;
return { json: { ...what, row: row || {}, ...again, notice, apply: false, next: 'none', result: again.apply ? '' : again.result, removeButtons: again.apply ? false : again.removeButtons, applied: false } };
""", ("telegram.js",)),
        code("Texto del botón", 2680, 200, """
const what = $json;
const row = what.row && what.row.id ? what.row : null;
let base = '';
if (row) {
  const full = { ...row, reason_labels: REASONS };
  base = row.on_failure_message && row.last_send_error ? sendFailureMessage(full, row.last_send_error) : approvalMessage(full, new Date());
}
const timed = ['enviando', 'rechazado'].includes(what.result);
const line = what.result ? resultLine(what.result, { when: timed ? madridTime(new Date()) : '' }) : '';
const editText = line ? (base ? withResult(base, line) : line) : '';
const hasMessage = String(what.chatId || '') !== '' && String(what.messageId || '') !== '';
return { json: { ...what, edit: Boolean(what.removeButtons && editText && hasMessage), edit_text: editText, help_text: row ? editHelpMessage(row) : '' } };
""", ("format.js", "decide.js"), notes="El mensaje se rehace desde la fila, sin botones y con la línea de lo que pasó."),
        answer_query("Responder botón", 2900, 200, "={{ $json.callbackId }}", "={{ $json.notice }}", "={{ $json.next === 'none' }}",
                     "Quita el reloj del botón y dice qué ha pasado. Si no hay nada que hacer, sale como alerta. Con ids falsos (pruebas) falla sin parar."),
        bool_if("¿Quitar botones?", 3120, 200, f"={{{{ {what}.edit }}}}", "Las dos salidas siguen."),
        telegram_edit("Quitar botones", 3340, 100, f"={{{{ {what}.chatId }}}}", f"={{{{ {what}.messageId }}}}", f"={{{{ {what}.edit_text }}}}",
                      "Mismo texto, sin botones y con lo que pasó. La línea final («✅ Aprobado y enviado») la pone IG 04 al enviar."),
        switch_if("¿Enviar?", 3560, 200, f"={{{{ {what}.next }}}}", "send"),
        call("Pedir envío", 3780, 100, "ig-send", f"={{{{ JSON.stringify({{ message_id: {what}.id, mode: 'manual' }}) }}}}",
             "IG 04 manda el borrador (o tu texto) si la ventana sigue abierta."),
        code("Fallo envío", 4000, 100, f"""
const raw = $json.error;
return {{ json: {{ id: {what}.id, fallo: {http_error_js()} }} }};
"""),
        switch_if("¿Editar?", 3780, 300, f"={{{{ {what}.next }}}}", "edit"),
        telegram("Pedir texto", 4000, 300, f"={{{{ {what}.chatId }}}}", f"={{{{ {what}.help_text }}}}",
                 "No espera: el texto lo coge este mismo flujo como otro update. forceReply para que responder sea fácil.",
                 force_reply=True, reply_to=f"={{{{ {what}.messageId }}}}", on_error="continueRegularOutput"),
        postgres("Guardar edición", 4220, 300, SQL_06_EDIT_PROMPT,
                 f"={{{{ [{what}.id, $json.result && $json.result.message_id !== undefined ? String($json.result.message_id) : ''] }}}}",
                 "Recuerda el mensaje de ayuda: con varias ediciones abiertas, hay que responder citándolo."),
        switch_if("¿Texto?", 1140, 520, "={{ $json.kind }}", "text"),
        postgres("Ediciones pendientes", 1360, 520, SQL_06_PENDING, "", "Todas las filas en esperando_edicion (solo hay un chat autorizado)."),
        code("Qué texto", 1580, 520, """
const update = $('Leer update').item.json;
const outcome = editOutcome({ text: update.text, replyTo: update.replyTo, pending: $json.pending || [], now: Date.now() });
return { json: { ...update, ...outcome } };
""", ("telegram.js",), notes="Una edición abierta: vale cualquier texto. Varias: hay que citar el mensaje de ayuda. Más de 1000 bytes: no se envía."),
        telegram("Responder texto", 1800, 520, "={{ $json.chatId }}", "={{ $json.notice }}", "Dice qué se ha hecho con el texto.", on_error="continueRegularOutput"),
        bool_if("¿Guardar texto?", 2020, 520, "={{ $('Qué texto').item.json.apply }}"),
        postgres("Guardar texto", 2240, 520, SQL_06_SAVE_TEXT,
                 "={{ [$('Qué texto').item.json.igMessageId, $('Qué texto').item.json.finalReply, $('Qué texto').item.json.bytes] }}",
                 "esperando_edicion → aprobado con tu texto en final_reply."),
        bool_if("¿Texto guardado?", 2460, 520, "={{ !!$json.id }}"),
        call("Pedir envío editado", 2680, 520, "ig-send", "={{ JSON.stringify({ message_id: $json.id, mode: 'manual' }) }}",
             "IG 04 marca el mensaje «✅ Enviado con tu texto»."),
        code("Fallo editado", 2900, 600, f"""
const raw = $json.error;
return {{ json: {{ id: $('Guardar texto').item.json.id, fallo: {http_error_js()} }} }};
"""),
        postgres("Deshacer aprobación", 4220, 100, SQL_06_UNDO, "={{ [$json.id, $json.fallo] }}",
                 "IG 04 no recibió la petición: la fila vuelve a esperando_aprobacion (tu texto editado se conserva)."),
        code("Texto fallo", 4440, 100, """
const config = $('Configuración').first().json;
const hint = 'No se ha enviado. ¿Está activo IG 04 (ig-send)? ¿n8n_url y la credencial Instagram API Key cuadran? Cuando esté, pulsa Aprobar otra vez: si editaste, sale tu texto.';
const row = { ...$json, reason_labels: REASONS };
const restore = Boolean($json.id) && String($json.telegram_chat_id || '') !== '' && String($json.telegram_message_id || '') !== '';
const label = $json.on_failure_message && $json.last_send_error ? 'Reintentar' : 'Aprobar';
const base = $json.on_failure_message && $json.last_send_error ? sendFailureMessage(row, $json.last_send_error) : approvalMessage(row, new Date());
const restoreText = restore ? withResult(base, resultLine('fallo_04', { when: madridTime(new Date()), tail: 'Pulsa ' + label + ' otra vez cuando esté.' })) : '';
return { json: { id: $json.id || '', chat_id: String(config.telegram_chat_id), text: failureMessage('IG 04 no recibió el envío', { ...$json, hint }, $json.fallo), restore, restore_chat_id: $json.telegram_chat_id || '', restore_message_id: $json.telegram_message_id || '', restore_text: restoreText, approve_label: label } };
""", ("format.js", "decide.js")),
        bool_if("¿Devolver botones?", 4660, 100, "={{ $json.restore }}", "Solo si la fila volvió a esperar y tiene mensaje. Las dos salidas siguen al aviso."),
        telegram_edit("Devolver botones", 4880, 20, "={{ $('Texto fallo').item.json.restore_chat_id }}", "={{ $('Texto fallo').item.json.restore_message_id }}",
                      "={{ $('Texto fallo').item.json.restore_text }}", "El mensaje vuelve a tener Aprobar (o Reintentar), Editar y Rechazar.",
                      buttons=[("={{ $('Texto fallo').item.json.approve_label }}", "={{ 'ig:approve:' + $('Texto fallo').item.json.id }}"), ("Editar", "={{ 'ig:edit:' + $('Texto fallo').item.json.id }}"), ("Rechazar", "={{ 'ig:reject:' + $('Texto fallo').item.json.id }}")]),
        tg_ready("¿Telegram? (fallo)", 5100, 100),
        telegram("Avisar fallo", 5320, 100, "={{ $('Texto fallo').item.json.chat_id }}", "={{ $('Texto fallo').item.json.text }}", on_error="continueRegularOutput"),
        switch_if("¿Ajeno?", 1360, 760, "={{ $json.kind }}", "foreign"),
        postgres("Registrar ajeno", 1580, 760, SQL_06_FOREIGN, "={{ [$json.callbackId ? 'boton' : 'texto', $json.chatId, $json.fromId, $json.reason] }}",
                 "Chat o persona no autorizados: no se contesta ni se toca ninguna fila. Solo queda apuntado."),
        bool_if("¿Botón no válido?", 1580, 900, "={{ $json.callbackId !== '' }}", "Del chat autorizado pero con un callback_data que no es ig:<acción>:<uuid>."),
        answer_query("Responder no válido", 1800, 900, "={{ $json.callbackId }}", "Botón no válido. No se hace nada.", True),
    ]
    c = {}
    link(c, "Telegram", "Configuración")
    link(c, "Probar botón", "Configuración")
    link(c, "Configuración", "Leer update")
    link(c, "Leer update", "¿Botón?")
    link(c, "¿Botón?", "Leer fila", 0)
    link(c, "¿Botón?", "¿Texto?", 1)
    link(c, "Leer fila", "Qué hacer")
    link(c, "Qué hacer", "¿Aplicar?")
    link(c, "¿Aplicar?", "Aplicar botón", 0)
    link(c, "¿Aplicar?", "Texto del botón", 1)
    link(c, "Aplicar botón", "¿Aplicado?")
    link(c, "¿Aplicado?", "Aplicado", 0)
    link(c, "¿Aplicado?", "Releer fila", 1)
    link(c, "Aplicado", "Texto del botón")
    link(c, "Releer fila", "Sin cambios")
    link(c, "Sin cambios", "Texto del botón")
    link(c, "Texto del botón", "Responder botón")
    link(c, "Responder botón", "¿Quitar botones?")
    link(c, "¿Quitar botones?", "Quitar botones", 0)
    link(c, "¿Quitar botones?", "¿Enviar?", 1)
    link(c, "Quitar botones", "¿Enviar?")
    link(c, "¿Enviar?", "Pedir envío", 0)
    link(c, "¿Enviar?", "¿Editar?", 1)
    link(c, "Pedir envío", "Fallo envío", 1)
    link(c, "Fallo envío", "Deshacer aprobación")
    link(c, "¿Editar?", "Pedir texto", 0)
    link(c, "Pedir texto", "Guardar edición")
    link(c, "¿Texto?", "Ediciones pendientes", 0)
    link(c, "¿Texto?", "¿Ajeno?", 1)
    link(c, "Ediciones pendientes", "Qué texto")
    link(c, "Qué texto", "Responder texto")
    link(c, "Responder texto", "¿Guardar texto?")
    link(c, "¿Guardar texto?", "Guardar texto", 0)
    link(c, "Guardar texto", "¿Texto guardado?")
    link(c, "¿Texto guardado?", "Pedir envío editado", 0)
    link(c, "Pedir envío editado", "Fallo editado", 1)
    link(c, "Fallo editado", "Deshacer aprobación")
    link(c, "Deshacer aprobación", "Texto fallo")
    link(c, "Texto fallo", "¿Devolver botones?")
    link(c, "¿Devolver botones?", "Devolver botones", 0)
    link(c, "¿Devolver botones?", "¿Telegram? (fallo)", 1)
    link(c, "Devolver botones", "¿Telegram? (fallo)")
    link(c, "¿Telegram? (fallo)", "Avisar fallo", 0)
    link(c, "¿Ajeno?", "Registrar ajeno", 0)
    link(c, "¿Ajeno?", "¿Botón no válido?", 1)
    link(c, "¿Botón no válido?", "Responder no válido", 0)
    return pack("IG 06 - Botones", nodes, c)


# ===========================================================================
# IG 07 - Resumen
# ===========================================================================

def flow_summary():
    nodes = [
        sticky("Notas resumen", 80, 40, "### IG 07 Resumen\n\nCada día a las 06:05 UTC (y con el botón). Cinco minutos después del «05 - Resumen» del correo para no pisarse.\n\n1. **Vencer**: ventanas de 24 h cerradas (ventana efectiva) → `vencido_ventana`; aprobaciones pasadas de `approval_due_at` → `aprobacion_vencida`.\n2. **Purgar** a `retention_days` (90): ig_messages, ig_comments (y su ig_actions en cascada), ig_contacts por `last_seen`, ig_graph_log, ig_auto_gate y log huérfano. `ig_events` a 14 días.\n3. **Contar** en una sola consulta y mandar `summaryMessage`.\n\n**Por qué un flujo propio y no ampliar el 05 del ep3**: el correo y Instagram tienen tablas, estados y bot distintos. Tocar el 05 obligaría a reimportarlo y a meter la credencial «Telegram Instagram» en el flujo del correo; si algo de Instagram falla (una tabla que aún no existe), el resumen del correo también fallaría. Así el ep3 queda intacto.\n\nLas ejecuciones de n8n guardan el texto de los DM: eso lo cubre la retención de ejecuciones de n8n, no esta purga.", 640, 400),
        node("Cada mañana", "n8n-nodes-base.scheduleTrigger", 1.3, 220, 260, {
            "rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 6, "triggerAtMinute": 5}]},
        }, "06:05 UTC (la instancia está en UTC)."),
        node("Resumen ahora", "n8n-nodes-base.manualTrigger", 1, 220, 460, {}, "Para enseñarlo en cámara sin esperar a mañana."),
        config_node(460, 360, ["retention_days", "telegram_chat_id"]),
        postgres("Vencer", 700, 360, SQL_07_EXPIRE, "", "Pasado el plazo o cerrada la ventana, no se envía. Se marca y se apunta."),
        postgres("Purgar", 940, 360, SQL_07_PURGE, f"={{{{ [{CFG}.retention_days] }}}}", "Retención: el plazo está en Configuración. ig_keywords no se toca."),
        postgres("Contar", 1180, 360, SQL_07_COUNT, "",
                 "Una consulta. Últimas 24 h salvo esperando y errores (los que siguen así). Atascados = a medio camino más de 1 h."),
        code("Redactar", 1420, 360, """
const counts = $input.first().json;
const purge = $('Purgar').first().json;
const text = summaryMessage({ ...counts, purged: purge.purged });
return { json: { text, chat_id: String($('Configuración').first().json.telegram_chat_id), counts: { ...counts, ...purge } } };
""", ("format.js",), each=False),
        tg_ready("¿Telegram?", 1660, 360),
        telegram("Mandar resumen", 1900, 300, "={{ $json.chat_id }}", "={{ $json.text }}", "Números en líneas cortas.", on_error="stopWorkflow"),
        postgres("Anotar resumen", 1900, 480, SQL_07_NO_TELEGRAM, "={{ [JSON.stringify($json.counts)] }}", "Sin chat configurado: el resumen queda en ig_actions."),
    ]
    c = {}
    link(c, "Cada mañana", "Configuración")
    link(c, "Resumen ahora", "Configuración")
    link(c, "Configuración", "Vencer")
    link(c, "Vencer", "Purgar")
    link(c, "Purgar", "Contar")
    link(c, "Contar", "Redactar")
    link(c, "Redactar", "¿Telegram?")
    link(c, "¿Telegram?", "Mandar resumen", 0)
    link(c, "¿Telegram?", "Anotar resumen", 1)
    return pack("IG 07 - Resumen", nodes, c)


def build_flows():
    return {
        "03-mensajes.json": flow_messages(),
        "04-enviar.json": flow_send(),
        "05-aprobacion.json": flow_ask(),
        "06-botones.json": flow_buttons(),
        "07-resumen.json": flow_summary(),
    }


# Para scripts/checks/sql_dm.py: nodo → (SQL, parámetros de ejemplo los pone la prueba).
SQL = {name: value for name, value in globals().items() if name.startswith("SQL_") and isinstance(value, str)}
