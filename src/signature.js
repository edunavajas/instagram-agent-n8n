// Firma de los webhooks de Meta (Instagram) y verificación del GET de suscripción.
//
// Meta firma cada POST con la cabecera X-Hub-Signature-256: sha256=<hex>, que es
// el HMAC-SHA256 del CUERPO TAL CUAL LLEGA (bytes crudos) con el App Secret.
// Si se firma el JSON ya parseado y vuelto a serializar, la firma no coincide
// (espacios, orden de claves, escapes é frente a é...). Por eso el nodo
// Webhook tiene activado Raw Body y aquí se trabaja con Buffer.
//
// Funciona igual en Node (scripts/check.js) y dentro de un Code node de n8n:
// el build quita la línea module.exports del final. require('crypto') solo
// dentro de la función (en el runner hace falta NODE_FUNCTION_ALLOW_BUILTIN=crypto).
// Ninguna función lanza excepciones: siempre devuelven {ok, reason}.

const SIGNATURE_PREFIX = 'sha256=';
const SIGNATURE_HEX_LENGTH = 64; // 32 bytes de SHA-256 en hexadecimal

function toBuffer(value) {
  if (value === undefined || value === null) return null;
  if (typeof Buffer !== 'undefined' && Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value);
  if (typeof value === 'string') return Buffer.from(value, 'utf8');
  return null;
}

// header puede llegar como string o, en algunos proxies, como array.
function readHeader(header) {
  if (Array.isArray(header)) return header.length === 1 ? readHeader(header[0]) : null;
  if (typeof header !== 'string') return null;
  return header.trim();
}

function verifySignature(rawBody, header, appSecret) {
  try {
    if (typeof appSecret !== 'string' || appSecret.length === 0) return { ok: false, reason: 'missing_secret' };
    const body = toBuffer(rawBody);
    if (!body || body.length === 0) return { ok: false, reason: 'empty_body' };
    const value = readHeader(header);
    if (!value) return { ok: false, reason: 'missing_header' };
    if (value.slice(0, SIGNATURE_PREFIX.length).toLowerCase() !== SIGNATURE_PREFIX) {
      return { ok: false, reason: 'bad_prefix' };
    }
    const hex = value.slice(SIGNATURE_PREFIX.length).toLowerCase();
    if (!/^[0-9a-f]+$/.test(hex)) return { ok: false, reason: 'malformed_header' };
    if (hex.length !== SIGNATURE_HEX_LENGTH) return { ok: false, reason: 'bad_length' };

    const crypto = require('crypto');
    const expected = crypto.createHmac('sha256', appSecret).update(body).digest();
    const received = Buffer.from(hex, 'hex');
    // timingSafeEqual lanza si las longitudes son distintas: se comprueba antes.
    if (received.length !== expected.length) return { ok: false, reason: 'bad_length' };
    if (!crypto.timingSafeEqual(received, expected)) return { ok: false, reason: 'mismatch' };
    return { ok: true };
  } catch (error) {
    return { ok: false, reason: 'error' };
  }
}

// GET de verificación: ?hub.mode=subscribe&hub.challenge=123&hub.verify_token=...
// n8n entrega la query con las claves tal cual ("hub.mode"); se acepta también
// la forma anidada {hub: {mode, ...}} por si algún parser la convierte.
function verifyChallenge(query, verifyToken) {
  try {
    if (!query || typeof query !== 'object') return { ok: false, reason: 'missing_query' };
    const nested = query.hub && typeof query.hub === 'object' ? query.hub : {};
    const pick = (key) => {
      const value = query['hub.' + key] !== undefined ? query['hub.' + key] : nested[key];
      return Array.isArray(value) ? value[0] : value;
    };
    const mode = pick('mode');
    const token = pick('verify_token');
    const challenge = pick('challenge');
    if (typeof verifyToken !== 'string' || verifyToken.length === 0) return { ok: false, reason: 'missing_verify_token' };
    // El valor de plantilla no puede verificar nunca: obliga a cambiarlo antes de conectar con Meta.
    if (verifyToken === 'cambia-esto') return { ok: false, reason: 'placeholder_verify_token' };
    if (mode !== 'subscribe') return { ok: false, reason: 'bad_mode' };
    if (typeof token !== 'string' || token.length === 0) return { ok: false, reason: 'missing_token' };
    if (challenge === undefined || challenge === null || String(challenge).length === 0) {
      return { ok: false, reason: 'missing_challenge' };
    }
    const a = Buffer.from(token, 'utf8');
    const b = Buffer.from(verifyToken, 'utf8');
    const crypto = require('crypto');
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return { ok: false, reason: 'token_mismatch' };
    return { ok: true, body: String(challenge) };
  } catch (error) {
    return { ok: false, reason: 'error' };
  }
}

module.exports = { verifySignature, verifyChallenge, SIGNATURE_PREFIX };
