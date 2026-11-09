'use strict';

// Capa 1 de la defensa contra inyección de prompt (DM de Instagram).
//
// Un DM es texto de un desconocido. Aquí se limpia (caracteres invisibles,
// NFKC, espacios), se recorta a 1000 caracteres y se buscan patrones de
// inyección en castellano e inglés, también ofuscados (letras separadas,
// leetspeak, homoglifos cirílicos o griegos, Unicode oculto).
//
// Importante: un DM con inyección NO se descarta. Sale con injection:true y
// decide.js lo manda SIEMPRE a aprobación humana, con aviso en Telegram.
// Lo único que se ignora es un mensaje sin texto (solo adjuntos o emoji).
//
// Sin require: este fichero se copia dentro de un Code node de n8n.

const MAX_CHARS = 1000;
// Tope de lo que se analiza (evita regex lentas con mensajes enormes).
const MAX_SCAN = 20000;

// --- Unicode invisible -------------------------------------------------------

// Caracteres de etiqueta (U+E0000–U+E007F): codifican ASCII invisible que el
// modelo SÍ lee ("ASCII smuggling").
const TAG_CHARS = /[\u{E0000}-\u{E007F}]/gu;
// Selectores de variación suplementarios: otra vía para esconder bytes.
const VS_SUPPLEMENT = /[\u{E0100}-\u{E01EF}]/gu;
// Controles de dirección (bidi): pueden invertir lo que se ve en pantalla.
const BIDI = /[\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g;
// Ancho cero y rellenos invisibles (sin U+200D, el ZWJ de los emoji).
const ZERO_WIDTH = /[\u00AD\u034F\u115F\u1160\u17B4\u17B5\u180B-\u180F\u200B\u200C\u2060-\u2064\u3164\uFEFF\uFFA0]/g;
// Controles C0/C1 salvo salto de línea y tabulador.
const CONTROL = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g;

function countMatches(text, regex) {
  const found = text.match(regex);
  return found ? found.length : 0;
}

// Quita lo invisible. Devuelve el texto limpio y lo que se encontró.
function stripInvisible(input) {
  let text = String(input);
  const found = {
    tags: countMatches(text, TAG_CHARS),
    vsSupplement: countMatches(text, VS_SUPPLEMENT),
    bidi: countMatches(text, BIDI),
    zeroWidth: countMatches(text, ZERO_WIDTH),
    vsRun: /[\uFE00-\uFE0F]{3,}/.test(text),
    hidden: '',
  };
  if (found.tags > 0) {
    // Se decodifica para enseñárselo a la persona que aprueba.
    found.hidden = Array.from(text)
      .filter((ch) => /[\u{E0020}-\u{E007E}]/u.test(ch))
      .map((ch) => String.fromCharCode(ch.codePointAt(0) - 0xE0000))
      .join('')
      .slice(0, 300);
  }
  // Una palabra partida por invisibles ("ig\u200Bnora") cuenta como ofuscación.
  found.zeroWidthInWord = /[\p{L}\p{N}][\u00AD\u034F\u200B\u200C\u2060-\u2064\uFEFF]+[\p{L}\p{N}]/u.test(text);
  text = text
    .replace(TAG_CHARS, '')
    .replace(VS_SUPPLEMENT, '')
    .replace(BIDI, '')
    .replace(ZERO_WIDTH, '')
    .replace(/\r\n?/g, '\n')
    .replace(/[\u2028\u2029]/g, '\n')
    .replace(/\t/g, ' ')
    .replace(CONTROL, '')
    // Selectores de variación (FE00–FE0F): solo se deja FE0F detrás de un emoji.
    .replace(/(\p{Extended_Pictographic})?([\uFE00-\uFE0F]+)/gu, (all, emoji) => (emoji ? emoji + '\uFE0F' : ''))
    // ZWJ (U+200D) solo entre emoji; entre letras se quita.
    .replace(/\u200D+/g, (zwj, offset, whole) => {
      const before = whole.slice(0, offset);
      const after = whole.slice(offset + zwj.length);
      const prevEmoji = /\p{Extended_Pictographic}\uFE0F?$/u.test(before);
      const nextEmoji = /^\p{Extended_Pictographic}/u.test(after);
      return prevEmoji && nextEmoji ? '\u200D' : '';
    });
  return { text, found };
}

function collapseSpaces(text) {
  return text
    .replace(/[^\S\n]+/g, ' ')
    .replace(/ *\n */g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// --- Formas para buscar patrones ---------------------------------------------

// Homoglifos frecuentes (cirílico y griego que parecen latinos), en minúscula.
const HOMOGLYPHS = {
  'а': 'a', 'в': 'b', 'е': 'e', 'ё': 'e', 'і': 'i', 'ї': 'i', 'ј': 'j', 'к': 'k', 'м': 'm', 'н': 'h', 'о': 'o',
  'р': 'p', 'с': 'c', 'т': 't', 'у': 'y', 'х': 'x', 'ѕ': 's', 'ԁ': 'd', 'ԛ': 'q', 'ԝ': 'w', 'ӏ': 'l', 'һ': 'h',
  'ɡ': 'g', 'ɑ': 'a', 'ı': 'i', 'ʏ': 'y', 'ɩ': 'i',
  'α': 'a', 'β': 'b', 'ε': 'e', 'ζ': 'z', 'η': 'n', 'ι': 'i', 'κ': 'k', 'μ': 'u', 'ν': 'v', 'ο': 'o', 'ρ': 'p',
  'τ': 't', 'υ': 'u', 'χ': 'x', 'ω': 'w', 'ς': 's', 'γ': 'y',
};
const LEET = { '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', '$': 's' };
// Variantes de < y > que el modelo puede leer como etiqueta.
const ANGLES = [
  [/[‹«〈⟨＜﹤❮〈]/g, '<'],
  [/[›»〉⟩＞﹥❯〉]/g, '>'],
  [/&(lt|#0*60|#x0*3c);?/gi, '<'],
  [/&(gt|#0*62|#x0*3e);?/gi, '>'],
];

function fold(text) {
  let out = text.toLowerCase();
  for (const [regex, value] of ANGLES) out = out.replace(regex, value);
  out = out.normalize('NFD').replace(/[\u0300-\u036F]/g, '');
  out = Array.from(out).map((ch) => HOMOGLYPHS[ch] || ch).join('');
  return out;
}

function leet(text) {
  return text.replace(/[013457@$]/g, (ch) => LEET[ch]);
}

// --- Patrones ----------------------------------------------------------------
// Se aplican al texto plegado: minúsculas, sin tildes, homoglifos latinizados.
// G = hueco de hasta 40 caracteres dentro de la misma frase.
const G = '[^.!?\\n]{0,40}?';

const PATTERNS = [
  ['ignorar_instrucciones', new RegExp('\\b(ignora|ignorad|ignore[ns]?|ignorar|olvida|olvidad|olvidate|olvidar|omite|omitir|saltate|saltarte|pasa de|no hagas caso|no obedezcas|desobedece|anula|descarta|prescinde de|deja de seguir)\\b' + G + '\\b(instrucciones|instruccion|reglas|normas|indicaciones|ordenes|directrices|pautas|prompt|restricciones|limitaciones|politicas|filtros|configuracion|programacion)\\b')],
  ['ignorar_instrucciones', new RegExp('\\b(ignore|disregard|forget|override|bypass|skip|neglect|abandon|discard|ignorez|oublie|oubliez|ignoriere|vergiss|ignorate|dimentica|esqueca|esquece)\\b' + G + '\\b(instructions?|rules|guidelines|directives|prompts?|programming|restrictions|constraints|polic(y|ies)|guardrails|filters|context|instrucoes|regras|istruzioni|regole|consignes|regles|anweisungen|regeln)\\b')],
  ['nuevas_instrucciones', /\b(nuevas|actualizadas|verdaderas|autenticas) (instrucciones|reglas|ordenes)\b|\b(new|updated|real|actual|true|revised) (instructions|rules|directives|system prompt)\b|\b(a partir de|desde) (ahora|este mensaje|ya|hoy)\b[^.!?\n]{0,20}?\b(eres|seras|actua|actuaras|responde|responderas|contesta|contestaras|escribe|escribiras|habla|hablaras|debes|deberas|tienes que|vas a|solo|siempre|nunca)\b|\bfrom now on\b|\bhenceforth\b/],
  ['revelar_prompt', /\b(system ?prompt|prompt (de|del) sistema|prompt inicial|prompt original|initial prompt|original prompt|system message|mensaje (de|del) sistema|instrucciones (del sistema|de sistema|iniciales|internas|ocultas|originales|secretas)|hidden (instructions|prompt)|internal (instructions|rules)|reglas internas|developer message|mensaje del desarrollador)\b/],
  ['revelar_prompt', new RegExp('\\b(revela|revelame|muestra|muestrame|ensena|ensename|dime|repite|repiteme|imprime|copia|escribe|pasame|dame|cuentame|traduce|resume|print|show|reveal|repeat|tell me|output|display|leak|give me|translate|summari[sz]e|dump|spell out)\\b' + G + '\\b(tu prompt|tus instrucciones|tus reglas|tu configuracion|your (prompt|instructions|rules|configuration|guidelines)|lo que (hay|tienes|pone) (arriba|antes|al principio)|el texto (de arriba|anterior|inicial)|(the )?(text|words|everything) above|everything before|the first (message|lines))')],
  ['revelar_prompt', /\b(what (are|were) your (instructions|rules)|cuales son tus (instrucciones|reglas|normas)|que instrucciones (tienes|te han dado))\b/],
  ['cambio_rol', /\b(you are now|you're now|now you are|ahora eres|eres ahora|ahora seras|actua(d|r)? (ahora )?como (si|un|una|el|la)|act as (a|an|if|my|the|though)|pretend (to be|you are|you're|that)|finge (que|ser)|fingir que|haz como si|haz de cuenta|simula (ser|que)|imagina que eres|imagine (you are|you're)|role-?play|role play|juego de rol|tu nuevo (rol|papel|nombre)|your new (role|name|persona)|cambia (tu|de) (rol|personalidad)|you will (now )?(act|respond|behave) as|responde como si (fueras|no tuvieras))\b/],
  ['cambio_rol', /\b((tu|your) (nueva tarea|tarea nueva|new task|nuevo objetivo|new goal|nueva mision)|eres (un|una) (asistente|bot|modelo|ia|inteligencia artificial|chatbot|llm)\b|(contesta|responde|habla|escribe|answer|reply|talk|write) (como|like|as) (si fueras|un|una|a|an|if)\b)/],
  ['revelar_prompt', /\b(el texto|todo|lo) que (tienes|hay|te han (dicho|dado|escrito|indicado)|te dijeron|has recibido) (antes|arriba|encima|al principio)\b|\btodo lo que te han (dicho|dado|indicado|escrito)\b|\b(everything|what) (you were|you've been|you have been) (told|given)\b/],
  ['forzar_salida', /\b(en|dentro de) (tu|la) (respuesta|contestacion|mensaje)\b[^.!?\n]{0,10}?\b(pon|di|escribe|incluye|menciona|anade|dile|indica|afirma)\b|\b(empieza|empiece|comienza|empezando|comenzando|start|begin|starting)\b[^.!?\n]{0,25}?\b(respuesta|reply|response|answer)\b|\b(tu|la) respuesta (empezando|comenzando) (por|con)\b/],
  ['jailbreak', /\b(jailbreak|jail break|do anything now|dan mode|modo dan|developer mode|modo desarrollador|modo dios|god mode|sin restricciones|sin censura|sin filtros|without (any )?(restrictions|filters|limits|censorship)|no restrictions|unfiltered|uncensored|unrestricted|prompt injection|inyeccion de prompt|evil (mode|confidant)|anti-?dan|stan mode|dude mode)\b/],
  ['delimitador_falso', /<\s*\/?\s*(mensaje_no_confiable|mensaje no confiable|fuentes_rag|fuentes rag|system|sistema|assistant|asistente|user|usuario|instructions?|instrucciones|prompt|developer|tool|context|contexto|im_start|im_end|inst|sys|s)\b/],
  ['delimitador_falso', /<\|[a-z_ ]{2,30}\|>|\[\/?(inst|system|sys|sistema)\]|<<\s*\/?\s*sys\s*>>/],
  ['delimitador_falso', /(^|\n)\s*#{1,6}\s*(system|sistema|instructions?|instrucciones|prompt|assistant|asistente|reglas|rules)\b|(^|\n)\s*(system|sistema|assistant|asistente|developer|desarrollador)\s*:|-{3,}\s*(system|sistema|instructions?|instrucciones)\b/],
  ['delimitador_falso', /\b(mensaje_no_confiable|fuentes_rag)\b|\b(end|fin) (of|del?) (the )?(user|usuario|input|datos|data|contexto|context|prompt)\b/],
  ['markdown_html', /<\s*(script|iframe|img|a|style|div|span|meta|link|object|embed|svg|form|input|body|html|br|p|b|i|code|pre)\b[^>]*>|<!--|!\[[^\]]*\]\([^)]*\)|\[[^\]\n]{1,80}\]\(\s*[^)\s]+\)|```/],
  ['dirigido_a_ia', /\b(si eres (una ia|un bot|un asistente|un modelo|chatgpt|una inteligencia artificial|un llm)|if you are (an ai|a bot|an assistant|a language model|an llm|chatgpt)|(nota|mensaje|aviso|instruccion) para (la ia|el asistente|el bot|el modelo|chatgpt)|note (to|for) (the )?(ai|assistant|bot|model|llm)|(dear|querido|querida|estimado) (ai|ia|chatgpt|assistant|asistente virtual|modelo|bot)\b|as an ai language model|como modelo de lenguaje)\b/],
  ['exfiltracion', /\b(incluye|anade|agrega|inserta|pega|adjunta|include|add|insert|append|paste|attach|embed)\b[^.!?\n]{0,60}?\b(enlace|link|url|vinculo|http)/],
  ['exfiltracion', /\b(envia|enviar|manda|mandar|reenvia|reenviar|comparte|filtra|exporta|sube|publica|send|forward|share|leak|export|upload|post|email)\b[^.!?\n]{0,50}?\b(contrasenas?|passwords?|tokens?|api ?keys?|credenciales|credentials|secretos?|secrets?|historial|conversaciones|conversations|chat history|lista de (clientes|usuarios|contactos)|base de datos|database|datos (internos|privados)|customer (data|list)|user data)\b/],
  ['exfiltracion', /\b(datos|telefonos?|correos?|emails?|direcciones|nombres|mensajes|conversaciones) de (otros|otras|los demas|todos los|todas las) (clientes|usuarios|seguidores|personas)\b|\b(other|all) (users|customers|clients)'?s? (data|emails|phone numbers|messages|info|information)\b/],
  ['exfiltracion', /\b(envia|manda|reenvia|send|forward|email|cc)\b[^!?\n]{0,60}?[\w.+-]+@[\w-]+\.[a-z]{2,}/],
  ['exfiltracion', /https?:\/\/\S*(\{|%7b|\[|<)\s*(data|datos|prompt|secret|token|historial|history|conversation|user)/],
  ['forzar_salida', /\b(responde|contesta|devuelve|genera|escribe|reply|respond|answer|return|output|print)\b[^.!?\n]{0,25}?\b(solo|unicamente|exclusivamente|exactamente|literalmente|textualmente|only|exactly|verbatim|literally)\b/],
  ['forzar_salida', /\b(responde|contesta|reply|answer|respond)\s+(que|that)\b|\b(repite|repeat)\s+(despues de mi|conmigo|after me|this|esto|exactamente)\b|\b(tu respuesta|your (reply|response|answer)) (debe|tiene que|must|should|has to|will) (ser|decir|contener|incluir|be|say|contain|include)\b/],
  ['forzar_salida', /\b(confirma|confirmame|asegura|asegurame|promete|prometeme|garantiza|garantizame|confirm|promise|guarantee)\b[^.!?\n]{0,30}?\b(devolveis|devolvereis|devuelven|reembolsais|reembolso|regalais|descuento del|100 ?%|refund|for free|discount)\b/],
  ['forzar_json', /\bjson\b|"?\b(category|confidence|draft_reply|reasoning|confianza|categoria|borrador)\b"?\s*[:=]|\b(consulta_frecuente|fuera_de_base|draft_reply)\b/],
  ['autoridad_falsa', /\b(soy (tu|vuestro|vuestra) (admin|administrador|administradora|desarrollador|desarrolladora|programador|programadora|creador|creadora|operador|operadora|jefe|jefa|dueno|duena)|soy (el|la) (admin|administrador|administradora|desarrollador|desarrolladora|programador|programadora|creador|creadora|operador|operadora) (de|del) (este |esta )?(bot|sistema|asistente|agente|ia|modelo|chat)|i'?a?m (your|the) (developer|admin|administrator|creator|operator|programmer)( of (this|the) (bot|system|assistant|ai))?|(mensaje|aviso|nota|alerta|orden) (oficial )?(del?|de la) (sistema|administrador|administracion|equipo de (instagram|meta|seguridad)|soporte de (instagram|meta))|(system|admin|administrator|security|openai|anthropic|deepseek) (message|notice|override|alert)|(message|mensaje) (from|de) (openai|anthropic|deepseek)|override (code|mode|protocol)|codigo de (anulacion|administrador))\b/],
  ['codificado', /\b(base ?64|rot ?13|rot-13|hexadecimal|decodifica|descodifica|decode|decrypt|descifra|desencripta)\b/],
];

// Palabras clave que se buscan en el texto "apretado" (solo letras, sin
// espacios ni signos): pillan "i g n o r a  l a s  i n s t r u c c i o n e s",
// "i.g.n.o.r.e", "1gn0r3" y similares.
const SQUEEZED = [
  'ignoratusinstrucciones', 'ignoralasinstrucciones', 'ignoratodaslasinstrucciones', 'ignoralasreglas', 'ignoratusreglas',
  'olvidatusinstrucciones', 'olvidalasinstrucciones', 'olvidatusreglas', 'olvidalasreglas',
  'ignorepreviousinstructions', 'ignoreallpreviousinstructions', 'ignoreallinstructions', 'ignoreyourinstructions',
  'ignoretheinstructions', 'ignoreprevious', 'disregardpreviousinstructions', 'disregardallpreviousinstructions',
  'disregardyourinstructions', 'forgetyourinstructions', 'forgetallpreviousinstructions', 'forgetpreviousinstructions',
  'systemprompt', 'promptdelsistema', 'promptdesistema', 'mensajedelsistema', 'developermode', 'mododesarrollador',
  'jailbreak', 'doanythingnow', 'youarenow', 'ahoraeres', 'eresahora', 'actuacomosi', 'mensajenoconfiable',
  'fuentesrag', 'revelatuprompt', 'revealyourprompt', 'nuevasinstrucciones', 'newinstructions', 'tusinstrucciones',
  'yourinstructions',
];

// Una palabra que mezcla latín con cirílico o griego: homoglifos.
function mixedScript(text) {
  return text.split(/[^\p{L}]+/u).some((word) => word.length > 1
    && /\p{Script=Latin}/u.test(word) && /[\p{Script=Cyrillic}\p{Script=Greek}]/u.test(word));
}

function pushOnce(list, name) {
  if (!list.includes(name)) list.push(name);
}

// Busca patrones en el texto ya limpio (sin invisibles, NFKC). Exportada para
// poder pasarla también por las fuentes del RAG si un flujo lo necesita.
function findInjection(clean) {
  const text = String(clean ?? '').slice(0, MAX_SCAN);
  const patterns = [];
  const folded = fold(text);
  const leeted = leet(folded);
  for (const [name, regex] of PATTERNS) {
    if (regex.test(folded) || regex.test(leeted)) pushOnce(patterns, name);
  }
  // Ofuscación: lo que solo aparece al juntar las letras.
  const squeezed = leeted.replace(/[^a-z]/g, '');
  const strong = ['ignorar_instrucciones', 'nuevas_instrucciones', 'revelar_prompt', 'cambio_rol', 'jailbreak', 'delimitador_falso'];
  if (SQUEEZED.some((word) => squeezed.includes(word)) && !patterns.some((name) => strong.includes(name))) {
    pushOnce(patterns, 'ofuscacion');
  }
  // Otros alfabetos (el plegado de homoglifos los rompe): sobre el texto sin plegar.
  const lower = text.toLowerCase();
  if (/(игнорируй|проигнорируй|забудь|игнорировать)[^.!?\n]{0,40}(инструкц|правил|указани)/.test(lower)
    || /(忽略|无视|忘记|忘掉|無視)[^。！？\n]{0,20}(指令|指示|规则|規則|提示)/.test(lower)) {
    pushOnce(patterns, 'ignorar_instrucciones');
  }
  // "DAN" en mayúsculas (sin gritar todo el mensaje: "ME DAN LA GUIA" no cuenta).
  const letters = text.replace(/[^\p{L}]/gu, '');
  const upperRatio = letters ? letters.replace(/[^\p{Lu}]/gu, '').length / letters.length : 0;
  if (/\bDAN\b/.test(text) && upperRatio < 0.5) pushOnce(patterns, 'jailbreak');
  if (mixedScript(text)) pushOnce(patterns, 'homoglifos');
  // Base64 / hexadecimal / escapes largos.
  const b64 = text.match(/[A-Za-z0-9+/]{40,}={0,2}/g) || [];
  if (b64.some((run) => /[0-9+/]/.test(run) && /[a-z]/.test(run) && /[A-Z]/.test(run))) pushOnce(patterns, 'codificado');
  if (/\b[0-9a-fA-F]{48,}\b/.test(text) || /(?:\b[0-9a-fA-F]{2}[ :,-]){23,}[0-9a-fA-F]{2}\b/.test(text)
    || /(\\x[0-9a-fA-F]{2}){8,}|(\\u[0-9a-fA-F]{4}){6,}|(%[0-9a-fA-F]{2}){12,}/.test(text)) {
    pushOnce(patterns, 'codificado');
  }
  if (/(https?:\/\/|www\.)\S{100,}/i.test(text)) pushOnce(patterns, 'url_larga');
  return patterns;
}

function inspectMessage(text, ctx) {
  const options = ctx || {};
  const maxChars = Number.isInteger(options.maxChars) && options.maxChars > 0 ? options.maxChars : MAX_CHARS;
  const raw = text === null || text === undefined ? '' : String(text);
  const stripped = stripInvisible(raw.length > MAX_SCAN * 2 ? raw.slice(0, MAX_SCAN * 2) : raw);
  const clean = collapseSpaces(stripped.text.normalize('NFKC'));

  const patterns = findInjection(clean);
  const hidden = stripped.found;
  if (hidden.tags > 0 || hidden.vsSupplement > 0 || hidden.bidi > 0 || hidden.vsRun || hidden.zeroWidth >= 3 || hidden.zeroWidthInWord) {
    patterns.unshift('unicode_oculto');
  }
  // Lo oculto en caracteres de etiqueta también se analiza.
  if (hidden.hidden) {
    for (const name of findInjection(hidden.hidden)) pushOnce(patterns, name);
  }

  const chars = Array.from(clean);
  const truncated = chars.length > maxChars;
  const sanitized = truncated ? chars.slice(0, maxChars).join('') : clean;
  const injection = patterns.length > 0;
  const result = { ignore: false, reason: '', injection, patterns, sanitized, truncated };
  if (hidden.hidden) result.hidden = hidden.hidden;

  // Sin letras ni números: solo adjuntos, emoji o signos. Se ignora y se
  // registra. Si hubo inyección (p. ej. todo escondido en Unicode), NO.
  if (!injection && !/[\p{L}\p{N}]/u.test(sanitized)) {
    result.ignore = true;
    result.reason = 'no_text';
  }
  return result;
}

module.exports = { inspectMessage, findInjection, stripInvisible, fold, MAX_CHARS };
