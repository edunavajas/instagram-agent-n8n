'use strict';

// Textos que salen a Telegram. Todo lo que viene de Instagram (usuario, comentario,
// mensaje, borrador) se escapa: el mensaje va con parse_mode HTML.
// Forma de todos los avisos (la misma que en el agente de correo): emoji y título
// en negrita, campos "Etiqueta: valor" uno por línea, bloques separados por una
// línea en blanco, el texto largo en una cita.

// Telegram corta en 4096. Los avisos se quedan en 3600 para que quepa la línea
// de resultado que se añade al pulsar un botón (withResult).
const TELEGRAM_MAX = 4096;
const MESSAGE_MAX = 3600;

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Recorta sin dejar medio emoji (un surrogate suelto lo rechaza Telegram).
function cut(value, max) {
  const text = String(value ?? '');
  if (text.length <= max) return text;
  let end = Math.max(0, max);
  const code = text.charCodeAt(end - 1);
  if (code >= 0xD800 && code <= 0xDBFF) end -= 1;
  return text.slice(0, end);
}

function quote(text) {
  return `<blockquote>${text}</blockquote>`;
}

// parts: líneas ya escapadas y, como mucho, un bloque largo
// { raw, limit, render(textoEscapado, recortado) }. Si no cabe, se recorta solo
// ese bloque. Cabecera y pie salen siempre enteros.
function fit(parts, max = MESSAGE_MAX) {
  const long = parts.find((part) => typeof part !== 'string');
  const render = (raw, trimmed) => parts
    .map((part) => (typeof part === 'string' ? part : part.render(escapeHtml(raw), trimmed)))
    .join('\n');
  if (!long) return render('', false);
  let raw = String(long.raw ?? '');
  let trimmed = false;
  if (long.limit && raw.length > long.limit) {
    raw = cut(raw, long.limit).trimEnd() + ' […]';
    trimmed = true;
  }
  const full = render(raw, trimmed);
  if (full.length <= max) return full;
  let keep = raw.length - (full.length - max) - 20;
  while (keep > 0) {
    const out = render(cut(raw, keep).trimEnd() + ' […]', true);
    if (out.length <= max) return out;
    keep -= Math.max(10, out.length - max);
  }
  return render('[…]', true);
}

// Hora de Madrid sin depender de ICU: UTC+1, y UTC+2 desde el último domingo
// de marzo hasta el último de octubre (a la 01:00 UTC, regla de la UE).
function lastSundayUtc(year, month) {
  const last = new Date(Date.UTC(year, month + 1, 0));
  return last.getUTCDate() - last.getUTCDay();
}

function madridLocal(date) {
  const when = new Date(date === undefined ? Date.now() : date);
  const year = when.getUTCFullYear();
  const summerStart = Date.UTC(year, 2, lastSundayUtc(year, 2), 1);
  const summerEnd = Date.UTC(year, 9, lastSundayUtc(year, 9), 1);
  const offset = when.getTime() >= summerStart && when.getTime() < summerEnd ? 2 : 1;
  return new Date(when.getTime() + offset * 3600000);
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

function madridTime(date) {
  const local = madridLocal(date);
  return pad2(local.getUTCHours()) + ':' + pad2(local.getUTCMinutes());
}

// "10/11 18:30", hora de Madrid. Para las ventanas, que cruzan de día.
function madridStamp(date) {
  const local = madridLocal(date);
  return `${pad2(local.getUTCDate())}/${pad2(local.getUTCMonth() + 1)} ${madridTime(date)}`;
}

function validDate(value) {
  if (value === undefined || value === null || value === '') return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

// "@usuario_demo" si lo conocemos; si no, el IGSID (en comentarios y mensajes
// sin nombre de usuario). Nunca queda vacío.
function userLine(row) {
  const username = String(row.username || '').trim().replace(/^@/, '');
  const igsid = String(row.igsid || '').trim();
  if (username) return `Usuario: @${escapeHtml(cut(username, 80))}${igsid ? ` (<code>${escapeHtml(cut(igsid, 40))}</code>)` : ''}`;
  return `Usuario: ${igsid ? `<code>${escapeHtml(cut(igsid, 40))}</code>` : '(desconocido)'}`;
}

// Lo que escribió el usuario. En ig_messages se guarda en text_normalized.
function userText(row) {
  return String(row.text ?? row.text_normalized ?? '').trim();
}

const CATEGORY_LABELS = {
  consulta_frecuente: 'consulta frecuente',
  revision: 'revisión',
  fuera_de_tema: 'fuera de tema',
  sensible: 'tema sensible',
};

function categoryLabel(category) {
  return CATEGORY_LABELS[category] || String(category || 'sin categoría').replace(/_/g, ' ');
}

function formatSources(sources) {
  const list = Array.isArray(sources) ? sources.slice(0, 3) : [];
  if (!list.length) return 'Sin fragmentos en la base de conocimiento.';
  return list.map((source) => {
    const title = cut(String(source.title || 'Documento'), 120);
    const when = source.updated_at ? ` (${String(source.updated_at).slice(0, 10)})` : '';
    const url = cut(String(source.canonical_url || source.url || ''), 200);
    return `• ${title}${when}${url ? `\n  ${url}` : ''}`;
  }).join('\n');
}

function loopHoldText(row) {
  const minutes = Number(row && row.loop_minutes);
  const span = Number.isFinite(minutes) && minutes > 0 ? `hace menos de ${minutes} min` : 'hace poco';
  return `Retenido por el freno anti-bucles: a este usuario ya se le contestó automáticamente ${span}. Revísalo antes de enviar.`;
}
const SENSITIVE_TEXT = 'Tema sensible: habla de precio, plazo, devolución o datos personales. Hace falta una persona.';
const WINDOW_RULE_TEXT = 'Instagram solo deja contestar dentro de las 24 h siguientes al último mensaje del usuario.';

// El título dice por qué está bloqueado. Mandan los motivos de decide.js
// (ig_messages.reasons); si no llegan, el principio del razonamiento, que
// decide.js escribe con estas mismas frases. Mismo orden de prioridad que decide.js.
const BLOCKED_TITLES = [
  ['inyeccion', /^El mensaje intenta dar instrucciones/, 'Bloqueado: intenta dar órdenes'],
  ['promesa', /^El borrador prometía/, 'Bloqueado: el borrador prometía dinero o plazos'],
  ['fuga_prompt', /^El borrador repetía instrucciones internas/, 'Bloqueado: el borrador repetía instrucciones internas'],
  ['enlace_ajeno', /^El borrador traía un enlace/, 'Bloqueado: el borrador traía un enlace o un contacto ajeno'],
  ['fuente_sospechosa', /^Una fuente de la base de conocimiento/, 'Bloqueado: fuente sospechosa en la base de conocimiento'],
];

function reasonList(value) {
  let list = value;
  if (typeof list === 'string') {
    try { list = JSON.parse(list); } catch { list = []; }
  }
  return Array.isArray(list) ? list.map((item) => String(item)) : [];
}

function blockedTitle(reasoning, reasons) {
  const codes = reasonList(reasons);
  const byCode = BLOCKED_TITLES.find(([code]) => codes.includes(code));
  if (byCode) return byCode[2];
  const text = String(reasoning || '').trim();
  const byText = BLOCKED_TITLES.find(([, regex]) => regex.test(text));
  return byText ? byText[2] : 'Bloqueado: revísalo antes de contestar';
}

// Motivos por los que no salió solo. labels: el mapa REASONS de decide.js
// (código → frase). Sin mapa, salen los códigos.
function reasonsBlock(reasons, labels) {
  const codes = reasonList(reasons);
  if (!codes.length) return [];
  const map = labels && typeof labels === 'object' ? labels : {};
  return ['', '<b>Motivos</b>', ...codes.slice(0, 8).map((code) => `• ${escapeHtml(cut(String(map[code] || code), 200))}`)];
}

// Línea de la ventana de 24 h. Con "now" avisa si ya se cerró.
function windowLine(row, now) {
  const expires = validDate(row.window_expires_at);
  if (!expires) return '';
  if (now !== undefined && now !== null) {
    const current = validDate(now);
    if (current && expires.getTime() <= current.getTime()) return `⌛ La ventana de 24 h se cerró el ${madridStamp(expires)}: ya no se podrá enviar.`;
  }
  return `⏳ Ventana de 24 h: se cierra el ${madridStamp(expires)} (hora de Madrid).`;
}

// Fila de ig_messages -> campos de approvalMessage. La usan el flujo 05 al mandar
// el aviso y el 04/06 al rehacerlo para quitarle los botones: sale igual.
function approvalFromRow(row) {
  let sources = Array.isArray(row.sources) ? row.sources : row.rag_sources;
  if (typeof sources === 'string') {
    try { sources = JSON.parse(sources); } catch { sources = []; }
  }
  return {
    username: row.username,
    igsid: row.igsid,
    text: row.text,
    text_normalized: row.text_normalized,
    category: row.category,
    confidence: row.confidence,
    draft_reply: row.draft_reply,
    reasoning: row.reasoning,
    window_expires_at: row.window_expires_at,
    loop_minutes: row.loop_minutes,
    reasons: reasonList(row.reasons),
    reason_labels: row.reason_labels,
    sources: Array.isArray(sources) ? sources : [],
    injection: row.injection === true || row.injection_flag === true,
    sensitive: row.sensitive === true || row.sensitive_flag === true,
    loop_hold: row.loop_hold === true,
  };
}

// Acepta la fila de ig_messages tal cual (rag_sources, injection_flag…) o ya mapeada.
function approvalMessage(input, now) {
  const row = approvalFromRow(input || {});
  const blocked = Boolean(row.injection);
  const parts = blocked
    ? [`🛑 <b>${blockedTitle(row.reasoning, row.reasons)}</b>`, userLine(row)]
    : [
      '📝 <b>Borrador para aprobar (Instagram)</b>',
      userLine(row),
      `Categoría: ${escapeHtml(categoryLabel(row.category))} · confianza ${Math.round(Number(row.confidence || 0) * 100)} %`,
    ];
  const window = windowLine(row, now);
  if (window) parts.push(window);
  const warnings = [];
  // El flujo 04 no lo mandó solo: ese usuario ya recibió una respuesta automática hace poco.
  if (row.loop_hold) warnings.push('🔁 ' + loopHoldText(row));
  if (row.sensitive) warnings.push('⚠️ ' + SENSITIVE_TEXT);
  if (warnings.length) parts.push('', ...warnings);
  if (row.reasoning) parts.push('', `Por qué: ${escapeHtml(cut(String(row.reasoning).trim(), 300))}`);
  parts.push(...reasonsBlock(row.reasons, row.reason_labels));
  parts.push('', '<b>Mensaje del usuario</b>', shortBlock(userText(row), 800));
  if (!blocked) {
    const draft = String(row.draft_reply || '').trim();
    parts.push('', '<b>Borrador</b>');
    parts.push(draft
      ? { raw: draft, render: (text, trimmed) => quote(text) + (trimmed ? '\n<i>Recortado para Telegram. El mensaje sale entero.</i>' : '') }
      : '(vacío: escribe tú la respuesta con Editar)');
  }
  parts.push('', '<b>Fuentes</b>', escapeHtml(formatSources(row.sources)));
  // Bloqueado = sin borrador. Lo que vea el usuario lo escribe una persona.
  if (blocked) parts.push('', 'Sin borrador y sin botón Aprobar: escribe tú la respuesta con Editar, o Rechazar.');
  return fit(parts);
}

// fit() admite un solo bloque largo. El texto del usuario va con este, recortado
// a mano; el borrador o la respuesta es el bloque largo de cada aviso.
function shortBlock(text, limit) {
  const clean = String(text || '').trim();
  return clean ? quote(escapeHtml(cut(clean, limit)) + (clean.length > limit ? ' […]' : '')) : '(sin texto)';
}

// Respuesta enviada. options.mode: 'auto' (el agente solo) o 'manual' (tú pulsaste
// Aprobar o Editar). options.dryRun: no salió nada a Instagram.
function sentNotice(row, options) {
  const opts = options || {};
  const auto = opts.mode === 'auto';
  const parts = [
    auto ? '🤖 <b>Respondido solo (Instagram)</b>' : '✅ <b>Respuesta enviada (Instagram)</b>',
    userLine(row),
    '',
    '<b>Mensaje del usuario</b>',
    shortBlock(userText(row), 600),
    '',
    auto ? '<b>Respuesta</b>' : '<b>Respuesta enviada</b>',
  ];
  const reply = String(row.final_reply || row.draft_reply || '').trim();
  parts.push(reply
    ? { raw: reply, limit: 1000, render: (text, trimmed) => quote(text) + (trimmed ? '\n<i>Recortada para Telegram. Salió entera.</i>' : '') }
    : '(sin texto)');
  if (opts.dryRun) parts.push('', '🧪 Simulado (DRY_RUN): no se envió nada a Instagram.');
  return fit(parts);
}

// No se envió porque pasaron 24 h desde el mensaje del usuario.
function windowClosedNotice(row) {
  const parts = ['⌛ <b>No se envió: la ventana de 24 h ya se cerró</b>', userLine(row)];
  const expires = validDate(row.window_expires_at);
  if (expires) parts.push(`La ventana se cerró el ${madridStamp(expires)} (hora de Madrid).`);
  parts.push('', '<b>Mensaje del usuario</b>', shortBlock(userText(row), 500));
  const draft = String(row.final_reply || row.draft_reply || '').trim();
  if (draft) parts.push('', '<b>Respuesta que no salió</b>', { raw: draft, limit: 800, render: (text, trimmed) => quote(text) + (trimmed ? '\n<i>Recortada para Telegram.</i>' : '') });
  parts.push('', WINDOW_RULE_TEXT + ' Si todavía merece la pena, contéstale tú desde la app de Instagram.');
  return fit(parts);
}

// Retenido por el freno anti-bucles: pasa a aprobación en vez de salir solo.
function heldNotice(row) {
  const parts = ['🔁 <b>Retenido por el freno anti-bucles</b>', userLine(row), '', loopHoldText(row)];
  const window = windowLine(row);
  if (window) parts.push(window);
  parts.push('', '<b>Mensaje del usuario</b>', shortBlock(userText(row), 500));
  return fit(parts);
}

const COMMENT_ERROR_HINTS = {
  rate_limit: 'Es un límite de Meta. Se reintenta solo, cada vez más espaciado, mientras el comentario tenga menos de 7 días.',
  window_closed: 'La ventana para contestar se cerró. No se reintenta.',
  invalid_comment: 'Meta no acepta ese comentario para respuesta privada (ya tenía una, pasaron 7 días o no es válido). No se reintenta.',
  permission: 'Falta un permiso o la cuenta tiene desactivado el acceso a mensajes. Revisa la app de Meta.',
  token: 'El token de Meta ya no vale. Genera uno nuevo y actualiza la credencial «Instagram Token».',
  not_found: 'Meta no encuentra ese ID o el token no lo ve. Revisa el ID de la cuenta y los permisos.',
  blocked: 'Esa persona no está disponible o te ha bloqueado. No se reintenta.',
  transient: 'Error temporal de Meta. Se reintenta solo.',
};

// Un comentario que no se pudo contestar. error: lo que devuelve classifyError
// (o un texto suelto). row: { comment_id, username, igsid, text, keyword, attempts, id }.
function commentErrorNotice(row, error) {
  const detail = error && typeof error === 'object' ? error : { message: error };
  const parts = ['⚠️ <b>Error con un comentario</b>', userLine(row)];
  if (row.keyword) parts.push(`Palabra clave: ${escapeHtml(cut(String(row.keyword), 80))}`);
  if (row.comment_id) parts.push(`Comentario: <code>${escapeHtml(cut(String(row.comment_id), 60))}</code>`);
  if (row.attempts) parts.push(`Intentos: ${escapeHtml(String(row.attempts))}`);
  parts.push('', shortBlock(row.text, 300));
  const codes = detail.code ? ` (código ${escapeHtml(String(detail.code))}${detail.subcode ? `/${escapeHtml(String(detail.subcode))}` : ''})` : '';
  parts.push('', `Error${codes}: <code>${escapeHtml(cut(String(detail.message || 'sin detalle').replace(/\s+/g, ' ').trim(), 400))}</code>`);
  const hint = COMMENT_ERROR_HINTS[detail.kind];
  if (hint) parts.push('', `<b>Qué hacer:</b> ${escapeHtml(hint)}`);
  return fit(parts);
}

// Aviso genérico de fallo. Corto y sin el texto del usuario.
function failureMessage(what, row, error) {
  const parts = [`⚠️ <b>Fallo: ${escapeHtml(cut(String(what), 200))}</b>`];
  if (row && (row.username || row.igsid)) parts.push(userLine(row));
  if (row && row.id) parts.push(`Fila: <code>${escapeHtml(row.id)}</code>`);
  parts.push(`Error: <code>${escapeHtml(cut(String(error || 'sin detalle'), 500))}</code>`);
  if (row && row.hint) parts.push('', `<b>Qué hacer:</b> ${escapeHtml(cut(String(row.hint), 1200))}`);
  return fit(parts);
}

const SEND_FAILURE_HINT = 'Queda en error_envio. Reintentar lo manda otra vez (tu texto si lo editaste), siempre que la ventana de 24 h siga abierta. Si nadie pulsa, vence como una aprobación.';

function sendFailureMessage(row, error) {
  return failureMessage('no se pudo enviar el mensaje', { ...row, hint: SEND_FAILURE_HINT }, error);
}

// Resumen diario. Secciones; las líneas a 0 no salen, salvo las importantes.
// counts (los rellena la consulta del flujo 07; ausentes = 0):
//   keyword_comments   comentarios con palabra clave
//   private_sent       respuestas privadas enviadas
//   auto_replied       mensajes contestados solos
//   approved           mensajes aprobados por ti (Aprobar o Editar)
//   rejected           rechazados por ti
//   expired_approvals  aprobaciones vencidas
//   expired_windows    ventanas de 24 h vencidas (no se envió)
//   blocked_injection  bloqueados por inyección
//   blocked_sensitive  retenidos por tema sensible
//   blocked_loop       retenidos por el freno anti-bucles
//   errors             con error
//   waiting            esperando aprobación
//   stuck              atascados más de 1 h
//   purged             borrados por retención
function summaryMessage(counts) {
  const data = counts || {};
  const n = (key) => (Number.isFinite(Number(data[key])) ? Math.max(0, Math.trunc(Number(data[key]))) : 0);
  const section = (title, rows) => {
    const shown = rows.filter(([, value, always]) => always || value > 0);
    return shown.length ? ['', `<b>${title}</b>`, ...shown.map(([label, value]) => `${label}: ${value}`)] : [];
  };
  const expired = n('expired_approvals') + n('expired_windows');
  const blocked = n('blocked_injection') + n('blocked_sensitive') + n('blocked_loop');
  const blockedDetail = [
    n('blocked_injection') ? `${n('blocked_injection')} inyección` : '',
    n('blocked_sensitive') ? `${n('blocked_sensitive')} sensibles` : '',
    n('blocked_loop') ? `${n('blocked_loop')} bucle` : '',
  ].filter(Boolean).join(', ');
  const expiredDetail = [
    n('expired_approvals') ? `${n('expired_approvals')} aprobaciones` : '',
    n('expired_windows') ? `${n('expired_windows')} ventanas de 24 h` : '',
  ].filter(Boolean).join(', ');
  const lines = [
    '📊 <b>Resumen de ayer · Instagram</b>',
    ...section('Comentarios', [
      ['Con palabra clave', n('keyword_comments')],
      ['Privados enviados', n('private_sent')],
    ]),
    ...section('Mensajes directos', [
      ['Contestados solos', n('auto_replied')],
      ['Aprobados por ti', n('approved')],
      ['Rechazados por ti', n('rejected')],
    ]),
    ...section('Pendiente de ti', [
      ['Esperando aprobación', n('waiting'), true],
    ]),
    ...section('Sin enviar', [
      [`Vencidos${expiredDetail ? ` (${expiredDetail})` : ''}`, expired],
      [`Bloqueados${blockedDetail ? ` (${blockedDetail})` : ''}`, blocked],
    ]),
    ...section('Problemas', [
      ['Con error (míralos)', n('errors'), true],
      ['Atascados más de 1 h', n('stuck')],
    ]),
  ];
  if (n('purged')) lines.push('', `Borrados por retención: ${n('purged')}`);
  const plural = (value, one, many) => `${value} ${value === 1 ? one : many}`;
  const todo = [
    n('waiting') ? `${n('waiting')} por aprobar` : '',
    n('errors') ? `${n('errors')} con error` : '',
    n('stuck') ? plural(n('stuck'), 'atascado', 'atascados') : '',
    expired ? plural(expired, 'vencido', 'vencidos') : '',
  ].filter(Boolean);
  lines.push('', todo.length ? `👉 Te toca: ${todo.join(', ')}.` : '✅ Nada pendiente de ti.');
  return lines.join('\n');
}

// Línea que se añade al mensaje con botones cuando se decide. telegram.js dice
// qué pasó (la clave); aquí está cómo se lee.
const RESULTS = {
  enviando: '⏳ Aprobado. Enviando…',
  enviado: '✅ Aprobado y enviado',
  enviado_editado: '✅ Enviado con tu texto',
  rechazado: '❌ Rechazado',
  editando: '✏️ Editando: escribe en este chat el texto que se enviará',
  fallo_envio: '⚠️ Falló el envío',
  fallo_04: '⚠️ Falló el envío: el flujo IG 04 no recibió la petición',
  ya_respondido: '✅ Ya estaba respondido. No se envía otra vez.',
  ya_rechazado: '❌ Ya estaba rechazado. No se envía nada.',
  vencido: '⌛ Venció el plazo de aprobación. No se envía nada.',
  vencido_ventana: '⌛ No se envió: la ventana de 24 h ya se cerró',
  retenido: '🔁 Retenido por el freno anti-bucles y aún sin pasar a aprobación. Este botón no envía nada.',
  sin_fila: 'ℹ️ Este mensaje ya no corresponde a ningún mensaje de Instagram.',
  otro: 'ℹ️ Este botón ya no hace nada.',
};
const RESULT_TAILS = {
  fallo_envio: 'Tienes Reintentar en el aviso de abajo.',
  fallo_04: 'Pulsa Aprobar otra vez cuando esté.',
};

function resultLine(kind, options = {}) {
  let line = RESULTS[kind] || RESULTS.otro;
  if (options.detail) line += ': ' + escapeHtml(cut(String(options.detail).replace(/\s+/g, ' ').trim(), 200));
  if (options.when) line += ' · ' + options.when;
  const tail = options.tail || RESULT_TAILS[kind];
  return tail ? `${line}. ${tail}` : line;
}

function withResult(base, line) {
  return cut(`${base}\n\n${line}`, TELEGRAM_MAX);
}

// Un botón de un mensaje que ya no tiene fila: se rehace con el texto plano que
// trae el propio botón (Telegram lo manda sin formato).
function plainMessage(text) {
  return escapeHtml(cut(String(text || '').trim(), 3000));
}

// Ayuda que manda IG 06 al pulsar Editar. Con una sola edición abierta vale
// cualquier texto del chat; con varias, hay que responder citando este mensaje.
function editHelpMessage(row) {
  const parts = ['✏️ <b>Escribe la respuesta</b>', userLine(row || {})];
  const window = windowLine(row || {});
  if (window) parts.push(window);
  parts.push(
    '',
    'Manda en este chat el texto exacto que recibirá por DM (máx. 1000 bytes: cada tilde o ñ cuenta 2 y cada emoji 4).',
    'Si tienes varias ediciones abiertas, responde citando este mensaje.',
  );
  return fit(parts);
}

module.exports = { approvalMessage, approvalFromRow, blockedTitle, sentNotice, windowClosedNotice, heldNotice, commentErrorNotice, summaryMessage, failureMessage, sendFailureMessage, editHelpMessage, resultLine, withResult, plainMessage, madridTime, madridStamp, formatSources, escapeHtml, cut, TELEGRAM_MAX, MESSAGE_MAX };
