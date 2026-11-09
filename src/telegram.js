'use strict';

// Botones y textos que llegan al bot de Telegram del episodio 4
// (credencial «Telegram Instagram»). El estado vive en Postgres (ig_messages):
// aquí solo se decide QUÉ transición pedir; la aplica IG 06 con
// UPDATE … WHERE id = $1 AND status = <fromStatus>. Si ese UPDATE no toca
// ninguna fila (doble clic, otra ejecución ganó), se vuelve a llamar a
// callbackOutcome con el estado real y sale un aviso sin acción.
//
// La ventana de 24 h NO se comprueba aquí: lo hace IG 04 justo antes de
// enviar. Por eso todo lo que acaba en envío lleva needsWindowCheck: true.
//
// Sin require: este fichero se copia dentro de un Code node de n8n.

const ACTIONS = ['approve', 'reject', 'edit'];
const CALLBACK_RE = /^ig:(approve|reject|edit):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;
const NIL_UUID = '00000000-0000-0000-0000-000000000000';
const CALLBACK_MAX_BYTES = 64;
const DM_MAX_BYTES = 1000;

function utf8Bytes(value) {
  let bytes = 0;
  for (const ch of String(value ?? '')) {
    const cp = ch.codePointAt(0);
    bytes += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
  }
  return bytes;
}

function idList(value) {
  return String(value ?? '').split(',').map((item) => item.trim()).filter(Boolean);
}

const NUMERIC_ID = /^-?\d{1,20}$/;

// Autorizado = el chat es el configurado (número; «cambia-esto» o vacío no
// autoriza a nadie) Y, si hay telegram_user_ids, quien pulsa está en la lista.
// Una lista con entradas que no son números no se ignora: no autoriza a nadie.
function isAuthorized(chatId, fromId, cfg) {
  const config = cfg || {};
  const ownerChat = String(config.telegram_chat_id ?? '').trim();
  if (!NUMERIC_ID.test(ownerChat)) return false;
  const chat = String(chatId ?? '').trim();
  if (!chat || chat !== ownerChat) return false;
  const users = idList(config.telegram_user_ids);
  if (users.length === 0) return true;
  if (users.some((user) => !NUMERIC_ID.test(user))) return false;
  const from = String(fromId ?? '').trim();
  return Boolean(from) && users.includes(from);
}

function parseCallbackData(data) {
  if (typeof data !== 'string' || utf8Bytes(data) > CALLBACK_MAX_BYTES) return null;
  const match = data.match(CALLBACK_RE);
  if (!match || match[2] === NIL_UUID) return null;
  return { action: match[1], igMessageId: match[2] };
}

// callback_data de los botones: 'ig:<acción>:<uuid>' (47 bytes, límite 64).
function buildCallbackData(action, igMessageId) {
  const data = 'ig:' + action + ':' + String(igMessageId ?? '').toLowerCase();
  return parseCallbackData(data) ? data : '';
}

// update de Telegram → { kind, action, igMessageId, text, chatId, fromId,
// messageId, callbackId, replyTo, reason }.
// kind: 'button' | 'text' | 'foreign' (no autorizado) | 'ignore'.
function parseUpdate(update, cfg) {
  const base = {
    kind: 'ignore', action: '', igMessageId: '', text: '', chatId: '', fromId: '',
    messageId: '', callbackId: '', replyTo: '', reason: '',
  };
  if (!update || typeof update !== 'object') return { ...base, reason: 'sin_update' };

  if (update.callback_query && typeof update.callback_query === 'object') {
    const query = update.callback_query;
    const message = query.message || {};
    const item = {
      ...base,
      chatId: message.chat && message.chat.id !== undefined ? String(message.chat.id) : '',
      fromId: query.from && query.from.id !== undefined ? String(query.from.id) : '',
      messageId: message.message_id !== undefined ? String(message.message_id) : '',
      callbackId: String(query.id || ''),
    };
    if (!isAuthorized(item.chatId, item.fromId, cfg)) return { ...item, kind: 'foreign', reason: 'no_autorizado' };
    const parsed = parseCallbackData(query.data);
    // Se devuelve callbackId igualmente: hay que contestar al botón para que
    // Telegram quite el reloj, aunque no se haga nada.
    if (!parsed) return { ...item, kind: 'ignore', reason: 'callback_invalido' };
    return { ...item, kind: 'button', action: parsed.action, igMessageId: parsed.igMessageId };
  }

  // Solo mensajes nuevos. Un mensaje editado, de canal o de un bot no cuenta.
  const message = update.message;
  if (!message || typeof message !== 'object') return { ...base, reason: 'tipo_no_usado' };
  const item = {
    ...base,
    chatId: message.chat && message.chat.id !== undefined ? String(message.chat.id) : '',
    fromId: message.from && message.from.id !== undefined ? String(message.from.id) : '',
    messageId: message.message_id !== undefined ? String(message.message_id) : '',
    replyTo: message.reply_to_message && message.reply_to_message.message_id !== undefined
      ? String(message.reply_to_message.message_id) : '',
  };
  if (message.from && message.from.is_bot) return { ...item, reason: 'bot' };
  if (!isAuthorized(item.chatId, item.fromId, cfg)) return { ...item, kind: 'foreign', reason: 'no_autorizado' };
  if (typeof message.text !== 'string') return { ...item, reason: 'sin_texto' };
  if (/^\/[a-z0-9_]+(@\w+)?(\s|$)/i.test(message.text.trim())) return { ...item, reason: 'comando' };
  return { ...item, kind: 'text', text: message.text };
}

// --- Matriz estado × botón ---------------------------------------------------

const NOTICES = {
  aprobado: 'Ya está aprobado y en cola de envío. No se envía otra vez.',
  enviando: 'Ya se está enviando. No se envía otra vez.',
  avisando: 'Aún se está preparando este aviso. Espera un momento.',
  respondido_auto: 'Ya se respondió solo. No se envía otra vez.',
  respondido_manual: 'Ya se respondió. No se envía otra vez.',
  rechazado: 'Ya estaba rechazado. No se envía nada.',
  aprobacion_vencida: 'Venció el plazo de aprobación. No se envía nada.',
  vencido_ventana: 'La ventana de 24 h de Instagram está cerrada: ya no se puede responder por DM.',
  retenido_bucle: 'Retenido por el freno anti-bucles y aún sin pasar a aprobación. Este botón no envía nada.',
};
// Estados en los que otra ejecución está trabajando: no se tocan los botones.
const IN_FLIGHT = ['avisando', 'aprobado', 'enviando'];
// Estados ya resueltos: se quitan los botones con la línea de resultado
// (clave de resultLine en format.js).
const STALE_RESULTS = {
  respondido_auto: 'ya_respondido',
  respondido_manual: 'ya_respondido',
  rechazado: 'ya_rechazado',
  aprobacion_vencida: 'vencido',
  vencido_ventana: 'vencido_ventana',
  // Una fila retenida recibe botones cuando IG 05 la pasa a esperando_aprobacion.
  // Si un botón la encuentra aún así, no llegó a aprobación: el botón no hace nada.
  retenido_bucle: 'retenido',
};
// Estados que admiten botones.
const OPEN = ['esperando_aprobacion', 'esperando_edicion', 'error_envio'];

function rowOf(state) {
  if (state && typeof state === 'object') {
    return {
      exists: Boolean(state.id || state.status),
      status: String(state.status || state.previous_status || ''),
      blocked: Boolean(state.injection_flag || state.blocked),
      hasFinal: String(state.final_reply || '').trim() !== '' || Boolean(state.has_final_reply),
      hasDraft: state.has_draft !== undefined ? Boolean(state.has_draft) : String(state.draft_reply || '').trim() !== '',
      expired: Boolean(state.expired),
    };
  }
  const status = String(state || '');
  return { exists: status !== '', status, blocked: false, hasFinal: false, hasDraft: true, expired: false };
}

// state: el estado (string) o la fila { status, injection_flag, draft_reply |
// has_draft, final_reply | has_final_reply, expired }. action: approve|reject|edit.
// Devuelve { apply, fromStatus, nextStatus, next, notice, result,
// removeButtons, needsWindowCheck }.
// apply=false → no se escribe nada en la base de datos.
function callbackOutcome(state, action) {
  const row = rowOf(state);
  const out = (fields) => ({
    apply: false, fromStatus: row.status, nextStatus: row.status, next: 'none', notice: '', result: '',
    removeButtons: false, needsWindowCheck: false, ...fields,
  });
  if (!ACTIONS.includes(action)) return out({ notice: 'Botón desconocido. No se hace nada.' });
  if (!row.exists) return out({ notice: 'Este mensaje ya no corresponde a ningún DM (¿borrado por retención?).', result: 'sin_fila', removeButtons: true });

  const status = row.status;
  if (OPEN.includes(status) && row.expired) {
    return out({ notice: NOTICES.aprobacion_vencida, result: 'vencido', removeButtons: true });
  }

  if (status === 'esperando_aprobacion' || status === 'error_envio') {
    if (action === 'approve') {
      // Bloqueado (inyección, fuga, promesa, enlace ajeno) o sin borrador:
      // Aprobar no tiene nada seguro que enviar. Editar y Rechazar siguen valiendo.
      if ((row.blocked || !row.hasDraft) && !row.hasFinal) {
        return out({ notice: row.blocked
          ? 'Este DM está bloqueado (intentó dar órdenes, filtraba instrucciones, prometía algo o traía un enlace ajeno). No se puede aprobar: usa Editar y escribe tú la respuesta.'
          : 'No hay borrador que enviar. Usa Editar y escribe tú la respuesta, o Rechazar.' });
      }
      return out({
        apply: true, nextStatus: 'aprobado', next: 'send', result: 'enviando', removeButtons: true, needsWindowCheck: true,
        notice: status === 'error_envio' ? 'Aprobado otra vez. Reintentando el envío.' : 'Aprobado. Enviando la respuesta.',
      });
    }
    if (action === 'reject') {
      return out({ apply: true, nextStatus: 'rechazado', next: 'reject', result: 'rechazado', removeButtons: true, notice: 'Rechazado. No se envía nada.' });
    }
    return out({
      apply: true, nextStatus: 'esperando_edicion', next: 'edit', result: 'editando', removeButtons: true,
      notice: 'Escribe ahora en este chat el texto que se enviará (máx. 1000 bytes).',
    });
  }

  if (status === 'esperando_edicion') {
    if (action === 'edit') return out({ notice: 'Ya estás editando: escribe el texto en este chat.' });
    if (action === 'reject') {
      return out({ apply: true, nextStatus: 'rechazado', next: 'reject', result: 'rechazado', removeButtons: true, notice: 'Rechazado. Se cancela la edición y no se envía nada.' });
    }
    // Aprobar durante la edición = enviar el borrador original, si se puede.
    if (row.blocked || !row.hasDraft) {
      return out({ notice: 'No hay borrador seguro que aprobar. Escribe el texto en este chat o pulsa Rechazar.' });
    }
    return out({
      apply: true, nextStatus: 'aprobado', next: 'send', result: 'enviando', removeButtons: true, needsWindowCheck: true,
      notice: 'Aprobado el borrador original. Se cancela la edición y se envía.',
    });
  }

  const notice = NOTICES[status] || `No se puede hacer eso ahora (estado: ${status}).`;
  if (IN_FLIGHT.includes(status)) return out({ notice });
  if (STALE_RESULTS[status]) return out({ notice, result: STALE_RESULTS[status], removeButtons: true });
  return out({ notice, result: 'otro' });
}

// --- Edición -----------------------------------------------------------------

// Texto libre del chat autorizado mientras hay DMs en esperando_edicion.
// pending: filas de ese chat en esperando_edicion
//   [{ id, telegram_edit_id?, approval_due_at? }].
// Se aplica si hay UNA sola fila viva (o si el texto responde, citándolo, al
// mensaje de edición de una de ellas). Más de 1000 bytes: aviso y no se envía.
function editOutcome(input) {
  const args = input || {};
  const now = args.now === undefined ? Date.now() : new Date(args.now).getTime();
  const text = String(args.text ?? '').replace(/\r\n?/g, '\n').trim();
  const out = (fields) => ({
    apply: false, igMessageId: '', finalReply: '', fromStatus: 'esperando_edicion', nextStatus: 'esperando_edicion',
    notice: '', reason: '', bytes: utf8Bytes(text), needsWindowCheck: false, ...fields,
  });
  const alive = (Array.isArray(args.pending) ? args.pending : []).filter((row) => {
    if (!row || !row.id) return false;
    if (row.approval_due_at === null || row.approval_due_at === undefined || row.approval_due_at === '') return true;
    const due = new Date(row.approval_due_at).getTime();
    return Number.isFinite(due) && Number.isFinite(now) && due > now;
  });
  if (!text) return out({ reason: 'vacio', notice: 'Texto vacío: no se envía nada.' });
  const replyTo = String(args.replyTo ?? '');
  let target = replyTo ? alive.find((row) => String(row.telegram_edit_id ?? '') === replyTo) : null;
  if (!target) {
    if (alive.length === 0) return out({ reason: 'sin_edicion', notice: 'No hay ninguna respuesta esperando texto. Ese mensaje no se envía a nadie.' });
    if (alive.length > 1) {
      return out({ reason: 'varias_ediciones', notice: `Hay ${alive.length} respuestas esperando texto. Contesta citando el mensaje de edición de la que quieras; este texto no se envía.` });
    }
    target = alive[0];
  }
  const bytes = utf8Bytes(text);
  if (bytes > DM_MAX_BYTES) {
    return out({ igMessageId: String(target.id), reason: 'demasiado_largo', notice: `Tu texto ocupa ${bytes} bytes y el máximo de Instagram es ${DM_MAX_BYTES} (cada tilde o ñ cuenta 2 y cada emoji 4). No se envía: escríbelo más corto.` });
  }
  return out({
    apply: true, igMessageId: String(target.id), finalReply: text, nextStatus: 'aprobado', reason: 'editado',
    notice: 'Texto guardado. Enviando tu respuesta.', needsWindowCheck: true,
  });
}

module.exports = { parseUpdate, callbackOutcome, editOutcome, isAuthorized, parseCallbackData, buildCallbackData, idList, utf8Bytes };
