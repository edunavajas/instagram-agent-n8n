'use strict';

// Todo lo que sabe el agente de la Graph API de Meta, sin llamarla: construir la
// petición (sin token: lo pone la credencial de n8n), clasificar los errores,
// responder en DRY_RUN, recortar a 1000 bytes y calcular reintentos y ventanas.
// Fuente de los códigos: notas de la documentación oficial (errores de Messenger
// e Instagram Messaging). El único flujo que habla con Meta es «IG 08 - Graph API».

const GRAPH_DM_MAX_BYTES = 1000;
const GRAPH_BACKOFF_MINUTES = [1, 2, 5, 15, 60, 240];
const GRAPH_OPS = ['public_reply', 'private_reply', 'send_dm', 'get_profile'];

function grPick(args, camel, snake) {
  if (!args) return '';
  const value = args[camel] !== undefined ? args[camel] : args[snake];
  return value === undefined || value === null ? '' : String(value).trim();
}

function grNeed(op, name, value) {
  if (!value) throw new Error(`graph.${op}: falta ${name}`);
  return value;
}

function utf8Bytes(text) {
  let total = 0;
  for (const char of String(text === undefined || text === null ? '' : text)) {
    const code = char.codePointAt(0);
    total += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  return total;
}

// Recorta a maxBytes bytes UTF-8 sin partir un carácter (ñ = 2 bytes, emoji = 4).
// Tampoco deja colgando un unión de emojis (ZWJ) ni un selector de variante.
function truncateUtf8(text, maxBytes) {
  const limit = Math.max(0, Math.trunc(Number(maxBytes)) || 0);
  let total = 0;
  let out = '';
  for (const char of String(text === undefined || text === null ? '' : text)) {
    const code = char.codePointAt(0);
    const size = code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
    if (total + size > limit) {
      return out.replace(/[‍️]+$/, '');
    }
    total += size;
    out += char;
  }
  return out;
}

// buildRequest('public_reply', { commentId, text }, cfg) -> { method, url, body, path }
// cfg = el nodo Configuración del flujo 08: graph_base, graph_version, ig_account_id
// (también se aceptan graphBase, graphVersion, igAccountId). path va sin host: es lo
// que se guarda en ig_graph_log.
function buildRequest(op, args, cfg) {
  const config = cfg || {};
  const base = String(config.graph_base || config.graphBase || 'https://graph.facebook.com').replace(/\/+$/, '');
  const version = String(config.graph_version || config.graphVersion || 'v27.0').replace(/^\/+|\/+$/g, '');
  const account = String(config.ig_account_id || config.igAccountId || '').trim();
  const make = (method, path, body) => ({ method, path: `/${version}/${path}`, url: `${base}/${version}/${path}`, body });
  const enc = encodeURIComponent;
  if (op === 'public_reply') {
    const commentId = grNeed(op, 'commentId', grPick(args, 'commentId', 'comment_id'));
    const text = grNeed(op, 'text', grPick(args, 'text', 'message'));
    return make('POST', `${enc(commentId)}/replies`, { message: text });
  }
  if (op === 'private_reply') {
    const commentId = grNeed(op, 'commentId', grPick(args, 'commentId', 'comment_id'));
    const text = grNeed(op, 'text', grPick(args, 'text', 'message'));
    return make('POST', `${enc(grNeed(op, 'ig_account_id', account))}/messages`, {
      recipient: { comment_id: commentId },
      message: { text: truncateUtf8(text, GRAPH_DM_MAX_BYTES) },
    });
  }
  if (op === 'send_dm') {
    const igsid = grNeed(op, 'igsid', grPick(args, 'igsid', 'recipient_id'));
    const text = grNeed(op, 'text', grPick(args, 'text', 'message'));
    return make('POST', `${enc(grNeed(op, 'ig_account_id', account))}/messages`, {
      recipient: { id: igsid },
      message: { text: truncateUtf8(text, GRAPH_DM_MAX_BYTES) },
    });
  }
  if (op === 'get_profile') {
    const igsid = grNeed(op, 'igsid', grPick(args, 'igsid', 'recipient_id'));
    const fields = grPick(args, 'fields', 'fields') || 'name,username';
    return make('GET', `${enc(igsid)}?fields=${enc(fields).replace(/%2C/gi, ',')}`, null);
  }
  throw new Error(`graph: operación desconocida (${String(op)})`);
}

function grParse(body) {
  if (typeof body === 'string') {
    try { return JSON.parse(body); } catch { return { error: { message: body.slice(0, 300) } }; }
  }
  return body;
}

// Tabla de errores: código y subcódigo de Meta -> qué hacer.
//   retryable = se puede volver a intentar más tarde (cola con backoff)
//   kind      rate_limit | window_closed | invalid_comment | permission | token |
//             not_found | blocked | transient | unknown
function classifyError(httpStatus, body) {
  const parsed = grParse(body);
  const error = parsed && typeof parsed === 'object' && parsed.error !== undefined ? parsed.error : parsed;
  const detail = error && typeof error === 'object' ? error : {};
  const code = Number.isFinite(Number(detail.code)) && detail.code !== null && detail.code !== '' ? Number(detail.code) : null;
  const subcode = Number.isFinite(Number(detail.error_subcode)) && detail.error_subcode !== null && detail.error_subcode !== '' ? Number(detail.error_subcode) : null;
  const status = Number(httpStatus) || 0;
  let message = '';
  if (typeof error === 'string') message = error;
  else if (detail.message) message = String(detail.message);
  message = message.replace(/\s+/g, ' ').trim().slice(0, 300);
  const out = (kind, retryable) => ({ ok: false, retryable, kind, code, subcode, message });

  if (code === 4 || code === 17 || code === 32) return out('rate_limit', true);
  if (code === 613) return subcode === 2018338 ? out('blocked', false) : out('rate_limit', true);
  if (code === 368) return out('blocked', false);
  if (code === 10) {
    return subcode === 2534022 || subcode === 2018278 ? out('window_closed', false) : out('permission', false);
  }
  if (code === 100) {
    if (subcode === 2534025) return out('invalid_comment', false);
    if (subcode === 33 || subcode === 2534014) return out('not_found', false);
    if (subcode === 2534037) return out('blocked', false);
    return out('unknown', false);
  }
  if (code === 200) return out('permission', false);
  if (code === 190 || code === 102) return out('token', false);
  if (code === 551) return out('blocked', false);
  if (code === 2 || code === 1) return out('transient', true);
  // Sin código conocido: manda el estado HTTP.
  if (code === null || status >= 500) {
    if (status === 429) return out('rate_limit', true);
    if (status >= 500 || status === 408 || status === 0) return out('transient', true);
    if (status === 401) return out('token', false);
    if (status === 403) return out('permission', false);
    if (status === 404) return out('not_found', false);
  }
  if (detail.is_transient === true) return out('transient', true);
  return out('unknown', false);
}

function grHash(text, seed) {
  let hash = seed >>> 0;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash.toString(16).padStart(8, '0');
}

function grStable(value) {
  if (Array.isArray(value)) return `[${value.map(grStable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${grStable(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value === undefined ? null : value);
}

// Respuesta falsa del modo DRY_RUN. El id es el mismo para la misma operación y
// los mismos argumentos (dry_<16 hex>): se puede repetir una prueba y comparar.
function dryRunResponse(op, args) {
  const source = `${op}|${grStable(args || {})}`;
  const id = `dry_${grHash(source, 2166136261)}${grHash(source, 84696351)}`;
  if (op === 'public_reply') return { id, dry_run: true };
  if (op === 'private_reply' || op === 'send_dm') {
    return { recipient_id: grPick(args, 'igsid', 'recipient_id') || `dry_user_${grHash(source, 7)}`, message_id: id, dry_run: true };
  }
  if (op === 'get_profile') return { id: grPick(args, 'igsid', 'recipient_id') || id, name: 'Usuario de prueba', username: 'usuario_demo', dry_run: true };
  return { id, dry_run: true };
}

// attempt = nº del intento que acaba de fallar (1, 2, 3…). 1, 2, 5, 15, 60 y
// 240 minutos; de ahí en adelante, siempre 240.
function backoffMinutes(attempt) {
  const number = Math.trunc(Number(attempt));
  if (!Number.isFinite(number) || number < 1) return GRAPH_BACKOFF_MINUTES[0];
  return GRAPH_BACKOFF_MINUTES[Math.min(number, GRAPH_BACKOFF_MINUTES.length) - 1];
}

// Cuándo volver a probar (ISO) tras el intento número "attempt".
function nextAttemptAt(attempt, now) {
  const base = now === undefined || now === null ? Date.now() : new Date(now).getTime();
  return new Date(base + backoffMinutes(attempt) * 60000).toISOString();
}

// Días que han pasado desde que se creó el comentario (con decimales).
function windowDays(createdAt, now) {
  const created = new Date(createdAt).getTime();
  const current = now === undefined || now === null ? Date.now() : new Date(now).getTime();
  if (Number.isNaN(created) || Number.isNaN(current)) return Infinity;
  return (current - created) / 86400000;
}

// ¿Sigue abierta la ventana de la respuesta privada (7 días por defecto)?
function withinPrivateWindow(createdAt, now, days) {
  const limit = days === undefined || days === null ? 7 : Number(days);
  return windowDays(createdAt, now) < limit;
}

module.exports = { buildRequest, classifyError, dryRunResponse, truncateUtf8, utf8Bytes, backoffMinutes, nextAttemptAt, windowDays, withinPrivateWindow, GRAPH_OPS, GRAPH_DM_MAX_BYTES };
