'use strict';

// Capa 3 y ruta de un DM. La decisión final no se fía del modelo.
//
// route:
//   'auto'     → IG 04 lo envía solo. Exige que se cumpla TODO (ver decide()).
//   'approval' → IG 05 lo manda a Telegram con Aprobar/Editar/Rechazar.
//   'ignore'   → no se contesta (ventana de 24 h cerrada, sin texto o spam).
// reasons: motivos fijos de por qué NO es 'auto' (ver REASONS). Vacío = auto.
//
// Un borrador "bloqueado" (inyección, fuga del prompt, promesa de dinero o
// plazos, enlace ajeno, fuente envenenada o JSON roto) se tira: draft = '' y
// injection = true, igual que en el episodio 3. En Telegram, Aprobar no envía
// nada si no hay texto: solo sale lo que escriba una persona con Editar.
//
// Sin require: este fichero se copia dentro de un Code node de n8n.

const CATEGORIES = ['consulta_frecuente', 'fuera_de_base', 'sensible', 'queja', 'saludo', 'spam'];
const KEYS = ['category', 'confidence', 'draft_reply', 'reasoning'];

// Motivos fijos (código → texto para el aviso de Telegram).
const REASONS = {
  ventana_cerrada: 'La ventana de 24 h de Instagram está cerrada (o no se conoce): ya no se puede responder por DM.',
  sin_texto: 'El mensaje no trae texto (solo adjuntos o emoji).',
  spam: 'El modelo lo clasificó como spam.',
  json_invalido: 'El modelo no devolvió un JSON válido con el esquema exacto.',
  inyeccion: 'El mensaje intenta dar instrucciones al sistema (inyección de prompt).',
  fuente_sospechosa: 'Una fuente del RAG contiene instrucciones o marcas: posible fuente envenenada.',
  categoria: 'La categoría no es consulta_frecuente.',
  sensible: 'Toca un tema sensible (precios, contratos, devoluciones, datos personales…).',
  confianza_baja: 'La confianza del modelo está por debajo del umbral.',
  rag_bajo: 'No hay fuentes o la mejor fuente puntúa por debajo de min_rag_score.',
  no_cubre: 'Las fuentes no cubren las palabras de la pregunta.',
  sin_borrador: 'No hay borrador.',
  promesa: 'El borrador promete dinero, descuentos o plazos que no están en las fuentes.',
  fuga_prompt: 'El borrador repite instrucciones internas o marcas del prompt.',
  enlace_ajeno: 'El borrador trae un enlace, dominio, correo, @usuario o teléfono que no está en las fuentes.',
  formato: 'El borrador trae markdown o HTML (en un DM se vería tal cual).',
  borrador_largo: 'El borrador pasa de 1000 bytes UTF-8 (límite de Instagram).',
  bucle: 'Freno anti-bucles: ya hubo una respuesta automática a esta persona hace poco.',
};

const DEFAULT_SENSITIVE = 'precio,precios,cuanto cuesta,cuánto cuesta,tarifa,tarifas,presupuesto,descuento,oferta,contrato,legal,devolucion,devolución,reembolso,garantia,garantía,factura,dni,iban,tarjeta,telefono,teléfono,direccion,dirección,datos personales,contraseña,password';
const MAX_BYTES = 1000;
const HOUR = 3600 * 1000;
const MAX_WINDOW_HOURS = 24;
// Tolerancia de reloj: una ventana que acaba más allá de 24 h + 5 min es un dato roto.
const SKEW_MS = 5 * 60 * 1000;

// --- Utilidades --------------------------------------------------------------

function normalize(value) {
  return String(value ?? '').normalize('NFKC').toLowerCase().normalize('NFD').replace(/[\u0300-\u036F]/g, '');
}

function utf8Bytes(value) {
  let bytes = 0;
  for (const ch of String(value ?? '')) {
    const cp = ch.codePointAt(0);
    if (cp < 0x80) bytes += 1;
    else if (cp < 0x800) bytes += 2;
    else if (cp < 0x10000) bytes += 3;
    else bytes += 4;
  }
  return bytes;
}

function escapeRegex(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Instante en ms. Acepta Date, número (ms; o segundos si es < 1e11) o texto ISO.
// Un ISO sin zona horaria se toma como UTC. Lo que no se entiende → NaN.
function toMs(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return NaN;
  if (value instanceof Date) return value.getTime();
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return NaN;
    return Math.abs(value) < 1e11 ? value * 1000 : value;
  }
  if (typeof value === 'string') {
    const text = value.trim();
    if (/^\d+(\.\d+)?$/.test(text)) return toMs(Number(text));
    if (!/^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}(:?\d{2})?)?$/i.test(text)) return NaN;
    const withZone = /[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(text) ? text.replace(' ', 'T') + 'Z' : text.replace(' ', 'T');
    return Date.parse(withZone);
  }
  return NaN;
}

function nowMs(now) {
  return now === undefined ? Date.now() : toMs(now);
}

// --- Ventana de 24 h ---------------------------------------------------------

// Fin de la ventana a partir del momento del mensaje del usuario. Nunca más de
// 24 h: no se usa human_agent ni la ventana de 7 días de Click-to-Direct.
function windowExpiry(tsIso, hours) {
  const start = toMs(tsIso);
  if (!Number.isFinite(start)) return null;
  let h = Number(hours);
  if (!Number.isFinite(h) || h <= 0) h = MAX_WINDOW_HOURS;
  h = Math.min(h, MAX_WINDOW_HOURS);
  const end = new Date(start + h * HOUR);
  return Number.isNaN(end.getTime()) ? null : end.toISOString();
}

// Abierta solo si expiresAt > now (estricto). Nulo, NaN, ilegible o una
// ventana que acaba más allá de 24 h desde ahora → cerrada, por seguridad.
function isWindowOpen(expiresAt, now) {
  const end = toMs(expiresAt);
  const current = nowMs(now);
  if (!Number.isFinite(end) || !Number.isFinite(current)) return false;
  if (end - current > MAX_WINDOW_HOURS * HOUR + SKEW_MS) return false;
  return end > current;
}

// Freno anti-bucles: true si la última respuesta automática a esta persona
// fue hace menos de loop_minutes. En el límite exacto ya no frena.
// Un lastAutoAt ilegible o en el futuro frena (por seguridad).
function isLoopHeld(lastAutoAt, now, minutes) {
  if (lastAutoAt === null || lastAutoAt === undefined || lastAutoAt === '') return false;
  const last = toMs(lastAutoAt);
  const current = nowMs(now);
  if (!Number.isFinite(last) || !Number.isFinite(current)) return true;
  const elapsed = current - last;
  if (elapsed < 0) return true;
  return elapsed < minutes * 60 * 1000;
}

// --- Salida del modelo -------------------------------------------------------

function llmText(llm) {
  if (llm === null || llm === undefined) return null;
  if (typeof llm === 'string') return llm;
  if (typeof llm !== 'object') return null;
  if (Array.isArray(llm.choices)) {
    const message = llm.choices[0] && llm.choices[0].message;
    return message && typeof message.content === 'string' ? message.content : null;
  }
  if (typeof llm.content === 'string' && !('category' in llm)) return llm.content;
  return llm;
}

// Esquema estricto: exactamente las cuatro claves, categoría permitida,
// confianza número (o texto numérico simple) entre 0 y 1, textos en string.
// JSON rodeado de prosa → inválido. Se admite una sola valla ```json.
function parseModelOutput(llm) {
  let value = llmText(llm);
  if (value === null) return { ok: false, error: 'sin_salida' };
  if (typeof value === 'string') {
    let raw = value.trim();
    if (raw.length > 20000) return { ok: false, error: 'demasiado_largo' };
    const fenced = raw.match(/^```(?:json)?\s*\n?([\s\S]*?)\n?\s*```$/i);
    if (fenced) raw = fenced[1].trim();
    try {
      value = JSON.parse(raw);
    } catch (error) {
      return { ok: false, error: 'no_parsea' };
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { ok: false, error: 'no_es_objeto' };
  const keys = Object.keys(value);
  const extra = keys.filter((key) => !KEYS.includes(key));
  if (extra.length > 0) return { ok: false, error: 'claves_extra:' + extra.slice(0, 5).join(',') };
  const missing = KEYS.filter((key) => !keys.includes(key));
  if (missing.length > 0) return { ok: false, error: 'faltan:' + missing.join(',') };
  if (typeof value.category !== 'string' || !CATEGORIES.includes(value.category.trim())) return { ok: false, error: 'categoria' };
  let confidence = value.confidence;
  if (typeof confidence === 'string' && /^\s*(0(\.\d{1,6})?|1(\.0{1,6})?|\.\d{1,6})\s*$/.test(confidence)) confidence = Number(confidence);
  if (typeof confidence !== 'number' || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) return { ok: false, error: 'confianza' };
  if (typeof value.draft_reply !== 'string') return { ok: false, error: 'draft_reply' };
  if (typeof value.reasoning !== 'string') return { ok: false, error: 'reasoning' };
  return {
    ok: true,
    value: {
      category: value.category.trim(),
      confidence,
      // Como en el ep3: tope de 4000 caracteres (lo que pase de 1000 bytes ya va a aprobación).
      draft: Array.from(value.draft_reply.trim()).slice(0, 4000).join(''),
      reasoning: value.reasoning.trim().slice(0, 1000),
    },
  };
}

// --- Comprobaciones del borrador y de las fuentes ----------------------------

function topicList(cfg) {
  const configured = String((cfg && cfg.sensitive_topics) || '').trim();
  return (configured || DEFAULT_SENSITIVE)
    .split(',')
    .map((item) => normalize(item).trim())
    .filter(Boolean);
}

// Por palabra o frase, sin tildes, con plural simple (s/es).
function findSensitive(text, topics) {
  const haystack = normalize(text);
  return topics.filter((topic) => {
    const body = topic.split(/[^a-z0-9]+/).filter(Boolean).map(escapeRegex).join('[^a-z0-9]+');
    if (!body) return false;
    return new RegExp('(^|[^a-z0-9])' + body + '(s|es)?($|[^a-z0-9])').test(haystack);
  });
}

const REFUND_PATTERNS = [
  /\b(te|os|le|les) (devolvemos|devolveremos|reembolsamos|reembolsaremos|abonamos|abonaremos|regalamos|descontamos)\b/,
  /\breembolso (aprobado|completo|integro|total|garantizado)\b/,
  /\b(devolucion|reembolso) (garantizad[ao]|asegurad[ao])\b/,
  /\b(hemos|he) (abonado|reembolsado|devuelto el dinero)\b/,
  /\b(refund(ed)?|we will refund|money back|full refund)\b/,
];
const PROMISE_PATTERNS = [
  /\d+([.,]\d+)?\s?(€|eur\b|euros?\b|usd\b|\$|dolares\b|libras\b|£)|(€|\$|£)\s?\d/,
  /\b\d{1,3}\s?%\s?(de )?(descuento|dto|off)\b|\b(descuento|rebaja|oferta) del? \d/,
  /\b(en|dentro de|antes de) (menos de )?(\d+|un|una|dos|tres|cuatro|cinco|seis|siete) (minutos|horas|h|dias|dias habiles|dias laborables|semanas|meses)\b/,
  /\b(te|os|le|les) (lo |la |los |las )?(enviamos|enviaremos|mandamos|mandaremos|entregamos|entregaremos|llegara|llegaran|tendras|tendreis) (manana|hoy|esta semana|en \d+)/,
  /\b(garantizamos|te garantizo|os garantizamos|te aseguro|os aseguro|te prometo|prometemos|aseguramos|we guarantee|we promise|i promise)\b/,
  /\b(within|in) (\d+|one|two|three) (hours|days|weeks)\b/,
  /\bplazo (de|maximo de) \d+/,
];

// Promesas de dinero o plazos. Un reembolso nunca se permite; el resto solo
// si el mismo trozo aparece tal cual en las fuentes.
function findPromise(draft, sourcesBlob) {
  const text = normalize(draft);
  if (REFUND_PATTERNS.some((regex) => regex.test(text))) return true;
  return PROMISE_PATTERNS.some((regex) => {
    const match = text.match(regex);
    return Boolean(match) && !sourcesBlob.includes(match[0].trim());
  });
}

const LEAK_PATTERNS = [
  /mensaje[\s_-]*no[\s_-]*confiable/,
  /fuentes[\s_-]*rag/,
  /\b(draft_reply|consulta_frecuente|fuera_de_base)\b/,
  /"?\b(category|confidence|reasoning)\b"?\s*:/,
  /\b(esquema exacto|reglas del borrador|prompt de sistema|system prompt|instrucciones de sistema|instrucciones del sistema|mis instrucciones|my instructions|my system)\b/,
  /\b(eres el asistente que clasifica|tu unica salida es un objeto json)\b/,
  /\b(soy un modelo de lenguaje|como modelo de lenguaje|as an ai language model|i am an ai language model)\b/,
  /<\s*\/?\s*(system|sistema|assistant|im_start|im_end)\b|<\|[a-z_]+\|>/,
];
const SHINGLE = 7;

function words(text) {
  return normalize(text).split(/[^a-z0-9]+/).filter(Boolean);
}

// Fuga del prompt: marcas, frases propias o 7 palabras seguidas del prompt de
// sistema (cfg.prompt, el mismo texto que se manda al modelo).
function leaksPrompt(draft, promptText) {
  const text = normalize(draft);
  if (LEAK_PATTERNS.some((regex) => regex.test(text))) return true;
  if (!promptText) return false;
  const promptWords = words(promptText);
  if (promptWords.length < SHINGLE) return false;
  const grams = new Set();
  for (let i = 0; i + SHINGLE <= promptWords.length; i += 1) grams.add(promptWords.slice(i, i + SHINGLE).join(' '));
  const draftWords = words(draft);
  for (let i = 0; i + SHINGLE <= draftWords.length; i += 1) {
    if (grams.has(draftWords.slice(i, i + SHINGLE).join(' '))) return true;
  }
  return false;
}

const TLDS = 'com|es|net|org|io|co|me|ly|app|dev|info|xyz|link|site|online|shop|store|biz|eu|us|uk|de|fr|it|pt|mx|ar|cl|ai|gg|tv|to|cc|ws|click|top|live|page|tk|ml|ga|cf|gq|ru|cn|in|example|cat|gal|eus|ink|lat|pro|club|vip|work|icu|su';
const URL_RE = /(?:https?|hxxps?):\/\/[^\s<>"'`]+|www\.[^\s<>"'`]+/giu;
const DOMAIN_RE = new RegExp('(?<![\\p{L}\\p{N}@.\\-/])(?:[\\p{L}\\p{N}](?:[\\p{L}\\p{N}-]{0,61}[\\p{L}\\p{N}])?\\.)+(?:' + TLDS + ')(?![\\p{L}\\p{N}-])(?:/[^\\s<>"\'`]*)?', 'giu');
const OBFUSCATED_RE = /[\p{L}\p{N}-]+\s*(\[\.\]|\(\.\)|\[punto\]|\(punto\)|\[dot\]|\(dot\)|\s+punto\s+|\s+dot\s+)\s*[a-z]{2,10}\b/giu;
const EMAIL_RE = /[\p{L}\p{N}._%+-]+@[\p{L}\p{N}-]+(?:\.[\p{L}\p{N}-]+)+/gu;
const HANDLE_RE = /(?:^|[^\p{L}\p{N}_@.])@([\p{L}\p{N}._]{2,30})/gu;
const PHONE_RE = /\+?\d[\d ().-]{7,}\d/g;

function cleanLink(value) {
  return value
    .toLowerCase()
    .replace(/^(?:https?|hxxps?):\/\//, '')
    .replace(/^www\./, '')
    .replace(/[.,;:!?)\]}»"'’]+$/, '')
    .replace(/\/+$/, '');
}

function linksOf(text) {
  const source = String(text ?? '').normalize('NFKC');
  const out = { links: [], emails: [], handles: [], phones: [], obfuscated: false };
  let rest = source;
  for (const match of source.match(EMAIL_RE) || []) {
    out.emails.push(match.toLowerCase());
    rest = rest.split(match).join(' ');
  }
  for (const match of rest.match(URL_RE) || []) {
    out.links.push(cleanLink(match));
    rest = rest.split(match).join(' ');
  }
  for (const match of rest.match(DOMAIN_RE) || []) out.links.push(cleanLink(match));
  for (const match of rest.matchAll(HANDLE_RE)) out.handles.push(match[1].toLowerCase().replace(/\.+$/, ''));
  for (const match of rest.match(PHONE_RE) || []) {
    const digits = match.replace(/\D/g, '');
    if (digits.length >= 9) out.phones.push(digits);
  }
  out.obfuscated = OBFUSCATED_RE.test(source);
  OBFUSCATED_RE.lastIndex = 0;
  // Dominio con homoglifos (latín mezclado con cirílico o griego): "exаmple".
  if (source.split(/\s+/).some((token) => token.includes('.') && /\p{Script=Latin}/u.test(token) && /[\p{Script=Cyrillic}\p{Script=Greek}]/u.test(token))) {
    out.obfuscated = true;
  }
  return out;
}

// true si el borrador trae algo que no aparece en las fuentes.
function hasForeignLinks(draft, sources) {
  const found = linksOf(draft);
  if (found.obfuscated) return true;
  const allowed = { links: new Set(), hosts: new Set(), emails: new Set(), handles: new Set(), phones: new Set() };
  for (const source of sources) {
    const blob = [source.canonical_url, source.title, source.content].map((v) => String(v ?? '')).join('\n');
    const own = linksOf(blob);
    own.links.forEach((link) => {
      allowed.links.add(link);
      allowed.hosts.add(link.split('/')[0]);
    });
    own.emails.forEach((email) => allowed.emails.add(email));
    own.handles.forEach((handle) => allowed.handles.add(handle));
    own.phones.forEach((phone) => allowed.phones.add(phone));
  }
  const linkOk = (link) => allowed.links.has(link) || (!link.includes('/') && allowed.hosts.has(link));
  return found.links.some((link) => !linkOk(link))
    || found.emails.some((email) => !allowed.emails.has(email))
    || found.handles.some((handle) => !allowed.handles.has(handle))
    || found.phones.some((phone) => !allowed.phones.has(phone));
}

const FORMAT_PATTERNS = [
  /\*\*[^*\n]+\*\*/, /__[^_\n]+__/, /(^|\n)\s{0,3}#{1,6}\s/, /\[[^\]\n]+\]\([^)\n]+\)/, /```|`[^`\n]+`/, /<\/?[a-z][^>]*>/i,
];

function hasFormatting(draft) {
  return FORMAT_PATTERNS.some((regex) => regex.test(draft));
}

// Instrucciones o marcas dentro de una fuente del RAG (fuente envenenada).
const SOURCE_PATTERNS = [
  /<\s*\/?\s*(mensaje_no_confiable|fuentes_rag|system|sistema|assistant|asistente|im_start|im_end)\b|<\|[a-z_]+\|>|\[\/?inst\]/,
  /\b(ignora|ignore|olvida|forget|disregard|omite|override)\b[^.\n]{0,40}\b(instrucciones|reglas|instructions|rules|prompt)\b/,
  /\b(system prompt|prompt de sistema|you are now|ahora eres|eres ahora|actua como si|act as if|from now on|a partir de ahora (eres|responde|debes))\b/,
  /\b(nota|instruccion|mensaje) para (la ia|el asistente|el modelo|el bot)\b|\bnote (to|for) (the )?(ai|assistant|model|bot)\b|\b(si eres|if you are) (una ia|un modelo|un asistente|an ai|a language model|an assistant)\b/,
  /\b(responde|contesta|reply|respond|answer)\b[^.\n]{0,25}\b(siempre|always)\b[^.\n]{0,25}\b(con|with)\b/,
];

function poisonedSources(sources) {
  return sources.some((source) => {
    const text = normalize([source.title, source.content].map((v) => String(v ?? '')).join('\n'));
    return SOURCE_PATTERNS.some((regex) => regex.test(text));
  });
}

// Como en el episodio 3: las palabras de 5+ letras de la pregunta tienen que
// aparecer en las dos primeras fuentes (2 aciertos, o 1 si la pregunta es corta).
function coversQuestion(body, sources) {
  const tokens = normalize(body).split(/[^a-z0-9]+/).filter((word) => word.length > 4);
  if (tokens.length === 0) return false;
  const blob = normalize((sources || []).slice(0, 2).map((source) => `${source.title || ''} ${source.content || ''}`).join(' '));
  const hits = tokens.filter((word) => blob.includes(word));
  return hits.length >= 2 || (hits.length === 1 && tokens.length <= 3);
}

function numberOr(value, fallback, min, max) {
  if (value === null || value === undefined || value === '') return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(max, Math.max(min, number));
}

// --- Decisión ----------------------------------------------------------------

function decide(input) {
  const args = input || {};
  const cfg = args.cfg || {};
  const inspection = args.inspection || {};
  // Umbral efectivo: nunca por debajo de 0,5 aunque Configuración diga otra cosa.
  const threshold = numberOr(cfg.confidence_threshold, 0.75, 0.5, 1);
  const minScore = numberOr(cfg.min_rag_score, 0.45, 0, 1);
  const loopMinutes = numberOr(cfg.loop_minutes, 10, 1, 24 * 60);
  const sources = (Array.isArray(args.sources) ? args.sources : []).filter((s) => s && typeof s === 'object');
  let topScore = Number(args.topScore);
  if (!Number.isFinite(topScore)) topScore = sources.length ? Math.max(...sources.map((s) => Number(s.score) || 0)) : 0;
  const message = String(inspection.sanitized ?? '');
  const injectionIn = Boolean(inspection.injection);
  const windowOpen = isWindowOpen(args.window && args.window.expiresAt, args.now);
  const loopHeld = isLoopHeld(args.lastAutoAt, args.now, loopMinutes);
  const parsed = parseModelOutput(args.llm);
  const model = parsed.ok ? parsed.value : { category: '', confidence: 0, draft: '', reasoning: '' };

  const base = {
    category: model.category || null,
    confidence: model.confidence,
    reasoning: model.reasoning,
    windowOpen,
    loopHeld,
    jsonError: parsed.ok ? '' : parsed.error,
  };
  // El título del aviso en Telegram (format.js, blockedTitle) sale del principio
  // del razonamiento, como en el episodio 3.
  const why = (reasons) => {
    if (reasons.includes('inyeccion')) return 'El mensaje intenta dar instrucciones al sistema.';
    if (reasons.includes('promesa')) return 'El borrador prometía una devolución, un descuento o un plazo que no está en la base de conocimiento.';
    if (reasons.includes('fuga_prompt')) return 'El borrador repetía instrucciones internas.';
    if (reasons.includes('enlace_ajeno')) return 'El borrador traía un enlace o un contacto que no está en las fuentes.';
    if (reasons.includes('fuente_sospechosa')) return 'Una fuente de la base de conocimiento trae instrucciones o marcas (posible fuente envenenada).';
    return '';
  };
  const finish = (route, reasons, extra) => {
    const blocked = Boolean(extra.blocked);
    const draft = route === 'ignore' || blocked ? '' : model.draft;
    let reasoning = model.reasoning;
    if (!parsed.ok) reasoning = 'El modelo no devolvió JSON válido (' + parsed.error + '). Pasa a una persona.';
    if (blocked) reasoning = (why(reasons) + ' No hay borrador: responde con Editar o rechaza. ' + reasoning).trim().slice(0, 1000);
    return {
      route,
      reasons,
      ...base,
      reasoning,
      draft,
      draftBytes: utf8Bytes(draft),
      sensitive: Boolean(extra.sensitive),
      injection: injectionIn || blocked,
      blocked,
    };
  };

  // 1. Ventana cerrada: no se puede contestar. Ni borrador ni botón Aprobar.
  //    Si además hubo inyección, se marca para que el flujo lo registre y avise.
  if (!windowOpen) {
    const reasons = ['ventana_cerrada'];
    if (injectionIn) reasons.push('inyeccion');
    return finish('ignore', reasons, { blocked: injectionIn });
  }
  // 2. Sin texto (y sin inyección): se ignora y se registra.
  if (inspection.ignore && !injectionIn) return finish('ignore', ['sin_texto'], {});

  const reasons = [];
  const add = (code) => { if (!reasons.includes(code)) reasons.push(code); };
  let blocked = false;
  if (injectionIn) { add('inyeccion'); blocked = true; }
  // JSON roto: sin borrador (no hay nada que aprobar), pero no "bloqueado".
  if (!parsed.ok) add('json_invalido');

  // 3. Spam limpio: no se contesta. Con inyección, a una persona.
  if (parsed.ok && model.category === 'spam' && !injectionIn) return finish('ignore', ['spam'], {});

  const topics = topicList(cfg);
  // Solo mensaje y borrador, como en el ep3: la FAQ llega en un único chunk que menciona
  // garantía, y mirar las fuentes mandaría a aprobación hasta «¿a qué hora abrís?».
  const sensitiveHits = findSensitive([message, model.draft].join('\n'), topics);
  const sensitive = sensitiveHits.length > 0;

  if (poisonedSources(sources)) { add('fuente_sospechosa'); blocked = true; }
  if (parsed.ok) {
    if (model.category !== 'consulta_frecuente') add('categoria');
    if (sensitive) add('sensible');
    if (model.confidence < threshold) add('confianza_baja');
  } else if (sensitive) {
    add('sensible');
  }
  if (sources.length === 0 || topScore < minScore) add('rag_bajo');
  if (!coversQuestion(message, sources)) add('no_cubre');
  if (parsed.ok) {
    const sourcesBlob = normalize(sources.map((s) => `${s.title || ''}\n${s.content || ''}`).join('\n'));
    if (!model.draft) add('sin_borrador');
    else {
      if (findPromise(model.draft, sourcesBlob)) { add('promesa'); blocked = true; }
      if (leaksPrompt(model.draft, cfg.prompt)) { add('fuga_prompt'); blocked = true; }
      if (hasForeignLinks(model.draft, sources)) { add('enlace_ajeno'); blocked = true; }
      if (hasFormatting(model.draft)) add('formato');
      if (utf8Bytes(model.draft) > MAX_BYTES) add('borrador_largo');
    }
  }
  if (loopHeld) add('bucle');

  return finish(reasons.length === 0 ? 'auto' : 'approval', reasons, { blocked, sensitive });
}

module.exports = { decide, isWindowOpen, windowExpiry, isLoopHeld, parseModelOutput, findSensitive, coversQuestion, hasForeignLinks, leaksPrompt, utf8Bytes, toMs, REASONS, CATEGORIES, DEFAULT_SENSITIVE };
