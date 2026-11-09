// Pruebas de src/signature.js (firma X-Hub-Signature-256 y GET de verificación).
// Exporta run(assert): lo llama scripts/check.js con require('assert').
// Vectores fijados con openssl:
//   printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'secreto-de-prueba-no-real'
//   printf 'abc' | openssl dgst -sha256 -hmac 'key'
// y el caso 2 de la RFC 4231 (clave "Jefe").

const path = require('path');
const crypto = require('crypto');
const { verifySignature, verifyChallenge } = require(path.join(__dirname, '..', '..', 'src', 'signature.js'));

const SECRET = 'secreto-de-prueba-no-real';
// Cuerpo tal cual lo mandaría Meta (compacto), con acentos, ¡, y un emoji de 4 bytes.
const BODY =
  '{"object":"instagram","entry":[{"id":"17841400000000001","time":1762700000,"changes":[{"field":"comments","value":{"from":{"id":"1234567890123456","username":"usuario_demo"},"comment_id":"18000000000000001","text":"GUIA ¡Qué guay! 🚀","media":{"id":"18050000000000001","media_product_type":"REELS"}}}]}]}';
const BODY_SIG = '56f4b018c80c2678ac49369e1df310f624d8fde52c839667e73c999b6955c9de';

function sign(body, secret) {
  return 'sha256=' + crypto.createHmac('sha256', secret).update(body).digest('hex');
}

module.exports = function run(assert) {
  // --- vectores conocidos ---
  assert.deepStrictEqual(verifySignature('abc', 'sha256=9c196e32dc0175f86f4b1cb89289d6619de6bee699e4c378e68309ed97a1a6ab', 'key'), { ok: true });
  assert.deepStrictEqual(
    verifySignature('what do ya want for nothing?', 'sha256=5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843', 'Jefe'),
    { ok: true },
  );
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG, SECRET), { ok: true });
  assert.deepStrictEqual(verifySignature(Buffer.from(BODY, 'utf8'), 'sha256=' + BODY_SIG, SECRET), { ok: true }, 'Buffer');
  assert.deepStrictEqual(verifySignature(new Uint8Array(Buffer.from(BODY, 'utf8')), 'sha256=' + BODY_SIG, SECRET), { ok: true }, 'Uint8Array');

  // --- mayúsculas / minúsculas y espacios alrededor de la cabecera ---
  assert.strictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG.toUpperCase(), SECRET).ok, true, 'hex en mayúsculas');
  assert.strictEqual(verifySignature(BODY, 'SHA256=' + BODY_SIG, SECRET).ok, true, 'prefijo en mayúsculas');
  assert.strictEqual(verifySignature(BODY, '  sha256=' + BODY_SIG + ' ', SECRET).ok, true, 'espacios alrededor');
  assert.strictEqual(verifySignature(BODY, ['sha256=' + BODY_SIG], SECRET).ok, true, 'cabecera como array de 1');

  // --- firma inválida ---
  const flipped = BODY_SIG.slice(0, -1) + (BODY_SIG.endsWith('e') ? 'f' : 'e');
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + flipped, SECRET), { ok: false, reason: 'mismatch' });
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG, SECRET + 'x'), { ok: false, reason: 'mismatch' }, 'otro secreto');
  assert.deepStrictEqual(verifySignature(BODY + ' ', 'sha256=' + BODY_SIG, SECRET), { ok: false, reason: 'mismatch' }, 'un espacio más');

  // --- longitud distinta / mal formada / prefijo erróneo / ausente ---
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG.slice(0, 62), SECRET), { ok: false, reason: 'bad_length' });
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG + '00', SECRET), { ok: false, reason: 'bad_length' });
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG.slice(0, 63) + 'z', SECRET), { ok: false, reason: 'malformed_header' });
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=', SECRET), { ok: false, reason: 'malformed_header' });
  assert.deepStrictEqual(verifySignature(BODY, 'sha1=' + BODY_SIG, SECRET), { ok: false, reason: 'bad_prefix' });
  assert.deepStrictEqual(verifySignature(BODY, BODY_SIG, SECRET), { ok: false, reason: 'bad_prefix' }, 'sin prefijo');
  assert.deepStrictEqual(verifySignature(BODY, 'sha256 ' + BODY_SIG, SECRET), { ok: false, reason: 'bad_prefix' });
  assert.deepStrictEqual(verifySignature(BODY, undefined, SECRET), { ok: false, reason: 'missing_header' });
  assert.deepStrictEqual(verifySignature(BODY, '', SECRET), { ok: false, reason: 'missing_header' });
  assert.deepStrictEqual(verifySignature(BODY, ['a', 'b'], SECRET), { ok: false, reason: 'missing_header' });
  assert.deepStrictEqual(verifySignature(BODY, 12345, SECRET), { ok: false, reason: 'missing_header' });

  // --- secreto y cuerpo vacíos ---
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG, ''), { ok: false, reason: 'missing_secret' });
  assert.deepStrictEqual(verifySignature(BODY, 'sha256=' + BODY_SIG, undefined), { ok: false, reason: 'missing_secret' });
  assert.deepStrictEqual(verifySignature('', sign('', SECRET), SECRET), { ok: false, reason: 'empty_body' });
  assert.deepStrictEqual(verifySignature(Buffer.alloc(0), sign('', SECRET), SECRET), { ok: false, reason: 'empty_body' });
  assert.deepStrictEqual(verifySignature(null, 'sha256=' + BODY_SIG, SECRET), { ok: false, reason: 'empty_body' });
  assert.deepStrictEqual(verifySignature({ object: 'instagram' }, 'sha256=' + BODY_SIG, SECRET), { ok: false, reason: 'empty_body' }, 'objeto, no bytes');

  // --- UTF-8 crudo frente a JSON re-serializado ---
  // Cuerpo "feo" pero legal: espacios, saltos, claves en otro orden, é escapada como é.
  const ugly = '{ "entry" : [ { "time": 1762700000, "id": "17841400000000001",\n  "changes": [ { "value": { "text": "GUIA \\u00a1Qu\\u00e9 guay! \\ud83d\\ude80", "comment_id": "18000000000000001" }, "field": "comments" } ] } ],\n "object": "instagram" }';
  const uglySig = sign(Buffer.from(ugly, 'utf8'), SECRET);
  assert.strictEqual(verifySignature(Buffer.from(ugly, 'utf8'), uglySig, SECRET).ok, true, 'crudo feo: pasa');
  const reserialized = JSON.stringify(JSON.parse(ugly));
  assert.notStrictEqual(reserialized, ugly);
  assert.deepStrictEqual(verifySignature(reserialized, uglySig, SECRET), { ok: false, reason: 'mismatch' }, 'JSON.stringify(body) falla');
  // El cuerpo compacto con emoji tal cual: JSON.stringify lo deja igual de bytes, pero
  // con indentación (lo que haría un pretty-print) ya no.
  assert.strictEqual(verifySignature(JSON.stringify(JSON.parse(BODY), null, 2), 'sha256=' + BODY_SIG, SECRET).ok, false);
  // latin1 en vez de utf8 (decodificar mal el binario) también falla.
  assert.strictEqual(verifySignature(Buffer.from(BODY, 'latin1'), 'sha256=' + BODY_SIG, SECRET).ok, false, 'latin1 falla');

  // --- nunca lanza ---
  assert.doesNotThrow(() => verifySignature(Symbol('x'), Symbol('y'), Symbol('z')));
  assert.strictEqual(verifySignature(Symbol('x'), 'sha256=' + BODY_SIG, SECRET).ok, false);

  // --- GET de verificación ---
  const q = { 'hub.mode': 'subscribe', 'hub.challenge': '1158201444', 'hub.verify_token': 'token-demo' };
  assert.deepStrictEqual(verifyChallenge(q, 'token-demo'), { ok: true, body: '1158201444' });
  assert.deepStrictEqual(verifyChallenge({ hub: { mode: 'subscribe', challenge: 42, verify_token: 'token-demo' } }, 'token-demo'), { ok: true, body: '42' });
  assert.deepStrictEqual(verifyChallenge({ ...q, 'hub.verify_token': 'otro' }, 'token-demo'), { ok: false, reason: 'token_mismatch' });
  assert.deepStrictEqual(verifyChallenge({ ...q, 'hub.verify_token': 'token-demoX' }, 'token-demo'), { ok: false, reason: 'token_mismatch' });
  assert.deepStrictEqual(verifyChallenge({ ...q, 'hub.mode': 'unsubscribe' }, 'token-demo'), { ok: false, reason: 'bad_mode' });
  assert.deepStrictEqual(verifyChallenge({ ...q, 'hub.challenge': '' }, 'token-demo'), { ok: false, reason: 'missing_challenge' });
  assert.deepStrictEqual(verifyChallenge({ 'hub.mode': 'subscribe', 'hub.challenge': '1' }, 'token-demo'), { ok: false, reason: 'missing_token' });
  assert.deepStrictEqual(verifyChallenge(q, ''), { ok: false, reason: 'missing_verify_token' });
  assert.deepStrictEqual(verifyChallenge({ 'hub.mode': 'subscribe', 'hub.verify_token': 'cambia-esto', 'hub.challenge': '1' }, 'cambia-esto'), { ok: false, reason: 'placeholder_verify_token' });
  assert.deepStrictEqual(verifyChallenge(undefined, 'token-demo'), { ok: false, reason: 'missing_query' });
  assert.strictEqual(verifyChallenge({ ...q, 'hub.verify_token': ['token-demo'] }, 'token-demo').ok, true, 'query repetida');
};
