'use strict';

// Palabras clave de los comentarios: normalizar, encontrar la regla, rotar la
// variante de la respuesta pública y montar el mensaje privado (máx. 1000 bytes).
// Sin I/O: las reglas (filas de ig_keywords) las pasa el flujo.

const KW_PRIVATE_MAX_BYTES = 1000;

// Minúsculas, sin diacríticos (NFKD: también pasa "𝐆𝐔𝐈𝐀" a "guia"), sin emojis ni
// símbolos (todo lo que no sea letra, marca de letra, número o espacio pasa a
// espacio; las marcas se conservan para no romper el japonés), sin caracteres invisibles y con los espacios colapsados.
function normalizeText(value) {
  return String(value === undefined || value === null ? '' : value)
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[​-‏⁠⁦-⁩﻿­︀-️⃣]/g, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{M}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function kwActive(rule) {
  return rule && rule.active !== false && String(rule.keyword || '').trim() !== '';
}

function kwHasMedia(rule) {
  return rule.media_id !== undefined && rule.media_id !== null && String(rule.media_id).trim() !== '';
}

// Devuelve la regla que encaja o null. Palabra completa o frase completa:
// "guia" encaja en "la guía, porfa" pero no en "guiar" ni en "guiado".
// Empate: la regla de una publicación concreta gana a la global; luego la
// palabra más larga; luego el orden en que llegaron.
function matchKeyword(text, rules, mediaId) {
  const haystack = ` ${normalizeText(text)} `;
  if (haystack.trim() === '' || !Array.isArray(rules)) return null;
  const media = mediaId === undefined || mediaId === null ? '' : String(mediaId).trim();
  let best = null;
  let bestScore = null;
  rules.forEach((rule, index) => {
    if (!kwActive(rule)) return;
    const needle = normalizeText(rule.keyword);
    if (!needle || !haystack.includes(` ${needle} `)) return;
    const specific = kwHasMedia(rule);
    if (specific && String(rule.media_id).trim() !== media) return;
    const score = [specific ? 1 : 0, needle.length, -index];
    if (bestScore === null || kwBetter(score, bestScore)) { best = rule; bestScore = score; }
  });
  return best;
}

function kwBetter(a, b) {
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

// Variante número "counter" (módulo el nº de variantes). Las vacías no cuentan.
function pickVariant(variants, counter) {
  const list = (Array.isArray(variants) ? variants : []).map((item) => String(item === null || item === undefined ? '' : item).trim()).filter(Boolean);
  if (!list.length) return '';
  const number = Math.trunc(Number(counter));
  const index = Number.isFinite(number) ? ((number % list.length) + list.length) % list.length : 0;
  return list[index];
}

function kwBytes(text) {
  let total = 0;
  for (const char of String(text)) {
    const code = char.codePointAt(0);
    total += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  return total;
}

// Recorta a maxBytes bytes UTF-8 sin partir un carácter.
function kwCut(text, maxBytes) {
  let total = 0;
  let out = '';
  for (const char of String(text)) {
    const code = char.codePointAt(0);
    const size = code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
    if (total + size > maxBytes) break;
    total += size;
    out += char;
  }
  return out;
}

// Mensaje privado de la regla: private_message y, si el enlace no aparece ya en
// él, el enlace al final. Si no cabe en 1000 bytes se recorta el texto, nunca el enlace.
function buildPrivateMessage(rule) {
  const message = String((rule && rule.private_message) || '').trim();
  const link = String((rule && rule.link) || '').trim();
  if (!link) return kwCut(message, KW_PRIVATE_MAX_BYTES).trimEnd();
  const full = message.includes(link) ? message : `${message}\n\n${link}`.trim();
  if (kwBytes(full) <= KW_PRIVATE_MAX_BYTES) return full;
  const suffix = `\n\n${link}`;
  const room = KW_PRIVATE_MAX_BYTES - kwBytes(suffix) - kwBytes('…');
  if (room <= 0) return kwCut(full, KW_PRIVATE_MAX_BYTES).trimEnd();
  const head = message.split(link).join('').trim();
  return `${kwCut(head, room).trimEnd()}…${suffix}`;
}

module.exports = { normalizeText, matchKeyword, pickVariant, buildPrivateMessage };
