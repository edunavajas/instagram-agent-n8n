-- Tablas del agente de Instagram (episodio 4).
-- Se aplican sobre el mismo PostgreSQL de los episodios 2 y 3, sin tocar
-- knowledge_* ni mail_*. Todas las tablas llevan el prefijo ig_.
-- Identificadores en inglés. Comentarios en español.
-- Idempotente: se puede aplicar dos veces (psql -f schema.sql).

-- gen_random_uuid() es nativo desde PostgreSQL 13: este esquema NO depende de
-- pgcrypto. Se intenta crear solo por compatibilidad con el episodio 3 y, si no
-- se puede (sin permisos), se sigue igual.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgcrypto no disponible (%): no hace falta, gen_random_uuid() es nativo', SQLERRM;
END;
$$;

-- updated_at se mantiene solo. Los flujos no tienen que acordarse.
CREATE OR REPLACE FUNCTION ig_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- Deduplicación de webhooks. Meta reintenta durante 36 h y los comentarios de
-- un anuncio pueden llegar repetidos. El flujo 01 hace
--   INSERT ... ON CONFLICT DO NOTHING RETURNING event_id
-- y solo sigue si devuelve fila. Se purga a los 14 días (flujo 07).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_events (
    event_id     text PRIMARY KEY,      -- comment_id (comentarios) o mid (mensajes)
    kind         text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ig_events DROP CONSTRAINT IF EXISTS ig_events_kind_check;
ALTER TABLE ig_events ADD CONSTRAINT ig_events_kind_check CHECK (kind IN ('comment', 'dm'));
CREATE INDEX IF NOT EXISTS ig_events_received_idx ON ig_events (received_at);

-- ---------------------------------------------------------------------------
-- Palabras clave de los comentarios. Se editan a mano (cualquier cliente SQL).
--   keyword          la palabra o frase; se compara normalizada (sin acentos,
--                    sin mayúsculas, sin emojis) y por palabra completa.
--   media_id         NULL = vale en todas las publicaciones; con valor, solo en esa.
--   public_replies   variantes de la respuesta pública; se rotan con variant_counter.
--   private_message  mensaje privado (máximo 1000 bytes en UTF-8).
--   link             enlace; si no aparece ya en private_message, se añade al final.
--   variant_counter  el flujo 02 lo incrementa en cada uso: counter % nº de variantes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_keywords (
    id               bigserial PRIMARY KEY,
    keyword          text NOT NULL,
    media_id         text,
    public_replies   text[] NOT NULL,
    private_message  text NOT NULL,
    link             text,
    active           boolean NOT NULL DEFAULT true,
    variant_counter  integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ig_keywords DROP CONSTRAINT IF EXISTS ig_keywords_keyword_check;
ALTER TABLE ig_keywords ADD CONSTRAINT ig_keywords_keyword_check CHECK (btrim(keyword) <> '');
ALTER TABLE ig_keywords DROP CONSTRAINT IF EXISTS ig_keywords_replies_check;
ALTER TABLE ig_keywords ADD CONSTRAINT ig_keywords_replies_check CHECK (cardinality(public_replies) >= 1);
ALTER TABLE ig_keywords DROP CONSTRAINT IF EXISTS ig_keywords_private_size_check;
ALTER TABLE ig_keywords ADD CONSTRAINT ig_keywords_private_size_check
    CHECK (btrim(private_message) <> '' AND octet_length(private_message) <= 1000);
-- Una misma palabra no se repite para la misma publicación (o para todas).
CREATE UNIQUE INDEX IF NOT EXISTS ig_keywords_keyword_media_uidx
    ON ig_keywords (lower(keyword), (COALESCE(media_id, '')));
CREATE INDEX IF NOT EXISTS ig_keywords_active_idx
    ON ig_keywords (active) WHERE active;

-- ---------------------------------------------------------------------------
-- Comentarios con palabra clave (o que se intentó procesar).
-- Estados (UPDATE ... WHERE status = '<el de antes>'):
--   procesando          reclamado por el flujo 02
--   sin_coincidencia    ninguna palabra clave activa encaja
--   respondido          respuesta pública y respuesta privada hechas
--   privado_enviado     solo la respuesta privada salió (la pública falló o no aplica)
--   reintento           error de límite: vuelve a probarse en next_attempt_at
--                       (flujo 02b) mientras created_at > now() - 7 días
--   error               error que no se reintenta (ventana, comentario no válido, token…)
--   duplicado_usuario   el mismo usuario ya recibió la privada en esa publicación
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_comments (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    comment_id          text NOT NULL UNIQUE,
    media_id            text,
    igsid               text,
    username            text,
    text                text,
    keyword_id          bigint REFERENCES ig_keywords(id) ON DELETE SET NULL,
    status              text NOT NULL,
    public_reply_id     text,
    private_message_id  text,
    attempts            integer NOT NULL DEFAULT 0,
    next_attempt_at     timestamptz,
    last_error          jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ig_comments DROP CONSTRAINT IF EXISTS ig_comments_status_check;
ALTER TABLE ig_comments ADD CONSTRAINT ig_comments_status_check CHECK (status IN (
    'procesando', 'sin_coincidencia', 'respondido', 'privado_enviado',
    'reintento', 'error', 'duplicado_usuario'
));
CREATE INDEX IF NOT EXISTS ig_comments_status_idx ON ig_comments (status);
CREATE INDEX IF NOT EXISTS ig_comments_created_idx ON ig_comments (created_at DESC);
CREATE INDEX IF NOT EXISTS ig_comments_retry_idx
    ON ig_comments (next_attempt_at) WHERE status = 'reintento';
CREATE INDEX IF NOT EXISTS ig_comments_user_media_idx ON ig_comments (igsid, media_id);
-- Una sola privada por (usuario, publicación): Meta solo deja una respuesta
-- privada por comentario, y aquí además no se le repite al mismo usuario.
CREATE UNIQUE INDEX IF NOT EXISTS ig_comments_private_once_uidx
    ON ig_comments (igsid, media_id)
    WHERE status IN ('respondido', 'privado_enviado');

DROP TRIGGER IF EXISTS ig_comments_touch ON ig_comments;
CREATE TRIGGER ig_comments_touch
    BEFORE UPDATE ON ig_comments
    FOR EACH ROW
    EXECUTE FUNCTION ig_touch_updated_at();

-- ---------------------------------------------------------------------------
-- Contactos: quien ha comentado una palabra clave. Una fila por IGSID.
-- La retención (90 días) se aplica sobre last_seen, no sobre first_seen.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_contacts (
    igsid       text PRIMARY KEY,
    username    text,
    keyword     text,
    media_id    text,
    comment_id  text,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ig_contacts_last_seen_idx ON ig_contacts (last_seen);

-- ---------------------------------------------------------------------------
-- Mensajes directos. mid es la clave de idempotencia.
-- Estados posibles. Cada flujo pasa de uno a otro con un UPDATE ... WHERE
-- status = '<el de antes>': si dos ejecuciones llegan a la vez, solo una gana.
--   03: procesando -> pendiente | ignorado | error_interno
--   04: pendiente/aprobado -> enviando -> respondido_auto | respondido_manual
--       | retenido_bucle | vencido_ventana | error_envio
--       (retenido_bucle = freno anti-bucles; vencido_ventana = pasaron 24 h
--        desde el mensaje del usuario y no se envía nada)
--   05: pendiente/retenido_bucle -> avisando -> esperando_aprobacion | error_aviso
--   06: esperando_aprobacion/esperando_edicion/error_envio -> aprobado
--       | rechazado | esperando_edicion | aprobacion_vencida
--   07: esperando_aprobacion/esperando_edicion -> aprobacion_vencida
--       pendiente/esperando_*/retenido_bucle/error_envio con la ventana
--       cerrada -> vencido_ventana
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_messages (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mid                  text NOT NULL UNIQUE,
    igsid                text NOT NULL,
    username             text,
    text_normalized      text,
    category             text,
    confidence           numeric(4,3),
    draft_reply          text,
    final_reply          text,
    reasoning            text,
    reasons              jsonb NOT NULL DEFAULT '[]'::jsonb,
    rag_sources          jsonb NOT NULL DEFAULT '[]'::jsonb,
    status               text NOT NULL,
    route                text,
    injection_flag       boolean NOT NULL DEFAULT false,
    sensitive_flag       boolean NOT NULL DEFAULT false,
    telegram_chat_id     text,
    telegram_message_id  text,
    telegram_edit_id     text,
    approval_due_at      timestamptz,
    window_expires_at    timestamptz,       -- received_at + 24 h
    reply_message_id     text,
    auto_replied_at      timestamptz,
    llm_tokens_in        integer,
    llm_tokens_out       integer,
    llm_ms               integer,
    received_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ig_messages_confidence_range
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
-- Si la tabla ya existía sin esta columna (motivos por los que un DM no sale solo).
ALTER TABLE ig_messages ADD COLUMN IF NOT EXISTS reasons jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE ig_messages DROP CONSTRAINT IF EXISTS ig_messages_status_check;
ALTER TABLE ig_messages ADD CONSTRAINT ig_messages_status_check CHECK (status IN (
    'procesando', 'pendiente', 'ignorado', 'error_interno',
    'enviando', 'respondido_auto', 'respondido_manual', 'retenido_bucle', 'error_envio',
    'avisando', 'esperando_aprobacion', 'esperando_edicion', 'aprobado', 'rechazado',
    'aprobacion_vencida', 'vencido_ventana', 'error_aviso'
));
ALTER TABLE ig_messages DROP CONSTRAINT IF EXISTS ig_messages_route_check;
ALTER TABLE ig_messages ADD CONSTRAINT ig_messages_route_check
    CHECK (route IS NULL OR route IN ('auto', 'approval', 'ignore'));

CREATE INDEX IF NOT EXISTS ig_messages_status_idx ON ig_messages (status);
CREATE INDEX IF NOT EXISTS ig_messages_igsid_auto_idx
    ON ig_messages (igsid, auto_replied_at DESC);
CREATE INDEX IF NOT EXISTS ig_messages_created_idx ON ig_messages (created_at DESC);
CREATE INDEX IF NOT EXISTS ig_messages_approval_due_idx
    ON ig_messages (approval_due_at)
    WHERE status = 'esperando_aprobacion';
CREATE INDEX IF NOT EXISTS ig_messages_window_idx
    ON ig_messages (window_expires_at)
    WHERE status IN ('pendiente', 'retenido_bucle', 'error_envio', 'esperando_aprobacion', 'esperando_edicion', 'aprobado');
CREATE UNIQUE INDEX IF NOT EXISTS ig_messages_telegram_uidx
    ON ig_messages (telegram_chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ig_messages_telegram_edit_uidx
    ON ig_messages (telegram_chat_id, telegram_edit_id)
    WHERE telegram_edit_id IS NOT NULL;

DROP TRIGGER IF EXISTS ig_messages_touch ON ig_messages;
CREATE TRIGGER ig_messages_touch
    BEFORE UPDATE ON ig_messages
    FOR EACH ROW
    EXECUTE FUNCTION ig_touch_updated_at();

-- ---------------------------------------------------------------------------
-- Freno anti-bucles atómico (IG 04, modo auto). Una fila por IGSID con la
-- última respuesta automática reservada. IG 04 la toma con
--   INSERT ... ON CONFLICT (igsid) DO UPDATE ... WHERE last_at <= now() - loop
-- en la MISMA sentencia que pasa el DM a 'enviando'. ON CONFLICT espera al otro
-- y mira la versión ya confirmada de la fila (no la foto del inicio), así que
-- de dos DM simultáneos del mismo usuario solo uno sale solo; el otro pasa a
-- 'retenido_bucle'. Con NOT EXISTS sobre ig_messages no basta: los dos verían
-- «ninguna respuesta todavía» y saldrían los dos. Si el envío falla, IG 04
-- borra su reserva. Se purga en IG 07 (retention_days).
-- (Añadido por el constructor del lado DM.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_auto_gate (
    igsid       text PRIMARY KEY,
    message_id  uuid,
    last_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ig_auto_gate_last_idx ON ig_auto_gate (last_at);

-- ---------------------------------------------------------------------------
-- Lo que el sistema hizo de verdad: enviar, responder, descartar, avisar.
-- message_id y comment_id vacíos = algo que no es de un mensaje ni de un
-- comentario (un botón de Telegram de otro chat). Nunca los dos a la vez.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_actions (
    id          bigserial PRIMARY KEY,
    message_id  uuid REFERENCES ig_messages(id) ON DELETE CASCADE,
    comment_id  uuid REFERENCES ig_comments(id) ON DELETE CASCADE,
    action      text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ig_actions DROP CONSTRAINT IF EXISTS ig_actions_one_parent_check;
ALTER TABLE ig_actions ADD CONSTRAINT ig_actions_one_parent_check
    CHECK (message_id IS NULL OR comment_id IS NULL);
CREATE INDEX IF NOT EXISTS ig_actions_message_idx ON ig_actions (message_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ig_actions_comment_idx ON ig_actions (comment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ig_actions_created_idx ON ig_actions (created_at);

-- ---------------------------------------------------------------------------
-- Registro de las llamadas a Meta que hace el flujo 08: todas las de DRY_RUN
-- (en vez de llamar) y las que fallaron de verdad. NUNCA guarda el token.
-- En dry-run, body lleva el texto del DM/respuesta que se habría enviado.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ig_graph_log (
    id           bigserial PRIMARY KEY,
    op           text NOT NULL,
    method       text,
    path         text,
    body         jsonb,
    dry_run      boolean NOT NULL DEFAULT true,
    ok           boolean,
    http_status  integer,
    error        jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ig_graph_log DROP CONSTRAINT IF EXISTS ig_graph_log_op_check;
ALTER TABLE ig_graph_log ADD CONSTRAINT ig_graph_log_op_check
    CHECK (op IN ('public_reply', 'private_reply', 'send_dm', 'get_profile'));
CREATE INDEX IF NOT EXISTS ig_graph_log_created_idx ON ig_graph_log (created_at DESC);
CREATE INDEX IF NOT EXISTS ig_graph_log_op_idx ON ig_graph_log (op, created_at DESC);

-- ---------------------------------------------------------------------------
-- Semilla de demostración: una palabra inventada (guia) y un enlace de ejemplo.
-- Cámbiala o desactívala (active = false) antes de ir a producción.
-- Si ya existe, no se toca (así tus cambios sobreviven a reaplicar el esquema).
-- ---------------------------------------------------------------------------
INSERT INTO ig_keywords (keyword, media_id, public_replies, private_message, link)
SELECT 'guia', NULL,
       ARRAY[
           '¡Te la mando ahora mismo por mensaje privado! 📩',
           '¡Hecho! Mira tu bandeja de mensajes, te acabo de escribir 📬',
           '¡Enviada! Revisa tus mensajes directos 🙌'
       ],
       E'Hola 👋 Aquí tienes la guía de demostración: https://example.com/guia-demo\n\nSi tienes cualquier duda, escríbeme por aquí.',
       'https://example.com/guia-demo'
WHERE NOT EXISTS (
    SELECT 1 FROM ig_keywords WHERE lower(keyword) = 'guia' AND media_id IS NULL
);
