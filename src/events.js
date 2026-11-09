'use strict';

// Normaliza el cuerpo de un webhook de Instagram en eventos propios.
// Acepta las dos formas del comentario:
//   - Unified Login:   entry[].changes[].value.comment_id
//   - Instagram Login: entry[].changes[].value.id
// y los mensajes en entry[].messaging[]. Llegan lotes con varias entries.
// Lo que no sirve se descarta con un motivo fijo (EVENT_DROP_REASONS) para
// poder contarlo y no perderlo en silencio. Sin I/O: no toca red ni base de datos.
//
// parseWebhook(payload, { igAccountId, igUsername, now? }) -> { events, dropped }
//   Event   = { kind: 'comment' | 'dm', id, igAccountId, from: { id, username? },
//               text, mediaId?, ts }
//   dropped = [{ reason, ref }]   ref = comment_id / mid / campo, para el registro

const EVENT_DROP_REASONS = ['echo', 'own_comment', 'own_message', 'read', 'reaction', 'postback', 'message_edit', 'unsupported_field', 'no_text', 'reply_to_reply', 'malformed'];

function evIsObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// Los ids de Meta son cadenas, pero un número también vale. Vacío = ''.
function evId(value) {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}

// entry.time llega en segundos (comentarios) o en milisegundos (mensajes).
function evIso(value, fallback) {
  const number = Number(value);
  if (value !== null && value !== '' && Number.isFinite(number) && number > 0) {
    const date = new Date(number < 1e11 ? number * 1000 : number);
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  if (typeof value === 'string' && value && Number.isNaN(number)) {
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  return fallback;
}

function evText(value) {
  return typeof value === 'string' ? value : '';
}

function parseWebhook(payload, ctx) {
  const options = evIsObject(ctx) ? ctx : {};
  const events = [];
  const dropped = [];
  const drop = (reason, ref) => dropped.push({ reason, ref: ref === undefined || ref === null ? '' : String(ref) });
  if (!evIsObject(payload) || !Array.isArray(payload.entry)) {
    drop('malformed', '');
    return { events, dropped };
  }
  const fallbackTs = evIso(options.now, new Date().toISOString());
  const ctxAccount = evId(options.igAccountId);
  const ownUsername = evText(options.igUsername).trim().replace(/^@/, '').toLowerCase();

  for (const entry of payload.entry) {
    if (!evIsObject(entry)) { drop('malformed', ''); continue; }
    const entryId = evId(entry.id);
    const account = entryId || ctxAccount;
    const isOwnId = (id) => Boolean(id) && (id === entryId || id === ctxAccount);
    const entryTs = evIso(entry.time, fallbackTs);

    // Instagram Login a veces manda el cambio suelto en la entry (sin "changes").
    const changes = Array.isArray(entry.changes) ? entry.changes
      : (evIsObject(entry.value) && entry.field ? [{ field: entry.field, value: entry.value }] : []);
    const messaging = Array.isArray(entry.messaging) ? entry.messaging : [];
    if (!changes.length && !messaging.length) drop('unsupported_field', entryId);

    for (const change of changes) {
      if (!evIsObject(change)) { drop('malformed', ''); continue; }
      if (change.field !== 'comments') { drop('unsupported_field', change.field); continue; }
      const value = change.value;
      if (!evIsObject(value)) { drop('malformed', 'comments'); continue; }
      const commentId = evId(value.comment_id) || evId(value.id);
      const from = evIsObject(value.from) ? value.from : {};
      const fromId = evId(from.id);
      if (!commentId || !fromId) { drop('malformed', commentId); continue; }
      const username = evText(from.username).trim();
      // Nuestras propias respuestas: por id de cuenta o por nombre de usuario.
      if (isOwnId(fromId) || (ownUsername && username.toLowerCase() === ownUsername)) { drop('own_comment', commentId); continue; }
      // Respuesta a otro comentario: se ignora (la privada y la pública cuelgan del de primer nivel).
      if (evId(value.parent_id)) { drop('reply_to_reply', commentId); continue; }
      const text = evText(value.text);
      if (!text.trim()) { drop('no_text', commentId); continue; }
      const event = { kind: 'comment', id: commentId, igAccountId: account, from: { id: fromId }, text, ts: entryTs };
      if (username) event.from.username = username;
      const mediaId = evId(evIsObject(value.media) ? value.media.id : '') || evId(value.media_id);
      if (mediaId) event.mediaId = mediaId;
      events.push(event);
    }

    for (const item of messaging) {
      if (!evIsObject(item)) { drop('malformed', ''); continue; }
      const message = evIsObject(item.message) ? item.message : null;
      const sender = evId(evIsObject(item.sender) ? item.sender.id : '');
      if (evIsObject(item.message_edit)) { drop('message_edit', evId(item.message_edit.mid)); continue; }
      if (evIsObject(item.read)) { drop('read', evId(item.read.mid)); continue; }
      if (evIsObject(item.reaction)) { drop('reaction', evId(item.reaction.mid)); continue; }
      if (evIsObject(item.postback)) { drop('postback', evId(item.postback.mid)); continue; }
      if (!message) { drop('unsupported_field', entryId); continue; }
      const mid = evId(message.mid);
      if (message.is_echo === true) { drop('echo', mid); continue; }
      if (!sender || !mid) { drop('malformed', mid); continue; }
      if (isOwnId(sender)) { drop('own_message', mid); continue; }
      const text = evText(message.text);
      // Borrados, adjuntos sueltos, stickers…: sin texto no hay nada que contestar.
      if (message.is_deleted === true || !text.trim()) { drop('no_text', mid); continue; }
      events.push({ kind: 'dm', id: mid, igAccountId: account, from: { id: sender }, text, ts: evIso(item.timestamp, entryTs) });
    }
  }
  return { events, dropped };
}

module.exports = { parseWebhook, EVENT_DROP_REASONS };
