'use strict';

// Banco de pruebas del agente de Instagram. Solo módulos built-in de Node.
//
//   node scripts/check.js
//
// 1. Carga cada src/*.js y comprueba que se puede inlinear en un Code node de n8n
//    (un solo module.exports, sin require ni import, salvo crypto dentro de signature.js).
// 2. Ejecuta cada scripts/checks/*.js. Cada uno exporta
//      module.exports = function (assert, ctx) { ... }
//    con ctx = { root, load(nombre) }. Si un check pide un módulo de src/ que aún no
//    existe, se salta con un aviso (no es un error: otro paso del trabajo lo creará).
// 3. Valida workflows/*.json (si los hay): sin secretos, credenciales por nombre con
//    id null y solo las del diseño, nodo Configuración, conexiones que existen y
//    ningún nodo Wait.
// Termina con «check ok» y código 0, o con el detalle y código 1.

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const warnings = [];
const failures = [];

class MissingModule extends Error {
  constructor(name) {
    super('falta src/' + name + '.js');
    this.moduleName = name;
  }
}

function load(name) {
  const file = path.join(root, 'src', String(name).replace(/\.js$/, '') + '.js');
  if (!fs.existsSync(file)) throw new MissingModule(name);
  return require(file);
}

function listFiles(dir, extension) {
  const folder = path.join(root, dir);
  if (!fs.existsSync(folder)) return [];
  return fs.readdirSync(folder).filter((name) => name.endsWith(extension)).sort();
}

function fail(label, error) {
  const detail = error && error.stack ? error.stack.split('\n').slice(0, 6).join('\n    ') : String(error);
  failures.push(label + '\n    ' + detail);
}

function guard(label, fn) {
  try {
    return fn();
  } catch (error) {
    fail(label, error);
    return undefined;
  }
}

// --- 1. src/*.js: se pueden pegar tal cual dentro de un Code node ---
const srcFiles = listFiles('src', '.js');
if (!srcFiles.length) warnings.push('no hay src/*.js todavía');
for (const name of srcFiles) {
  guard('src/' + name, () => {
    const text = fs.readFileSync(path.join(root, 'src', name), 'utf8');
    const lines = text.split('\n');
    const exportLines = lines.filter((line) => line.startsWith('module.exports'));
    assert.strictEqual(exportLines.length, 1, 'tiene que haber un único module.exports, en una sola línea que empiece por module.exports (el build lo quita)');
    assert.ok(/^module\.exports = \{.*\};$/.test(exportLines[0]), 'module.exports = { … }; en una sola línea');
    assert.ok(!/^\s*(import|export)\s/m.test(text), 'sin import/export de módulos ES');
    assert.ok(!/process\.env|\$env/.test(text), 'sin variables de entorno');
    // require solo en signature.js y solo para crypto, dentro de una función (sangrado).
    const requires = lines.filter((line) => /\brequire\s*\(/.test(line) && !line.trim().startsWith('//'));
    if (name === 'signature.js') {
      for (const line of requires) assert.ok(/require\(['"]crypto['"]\)/.test(line) && /^\s+/.test(line), 'signature.js: solo require(\'crypto\') dentro de la función');
    } else {
      assert.deepStrictEqual(requires, [], 'sin require');
    }
    // Se puede cargar de verdad.
    delete require.cache[require.resolve(path.join(root, 'src', name))];
    require(path.join(root, 'src', name));
  });
}

// --- 2. scripts/checks/*.js ---
const ctx = { root, load };
const checkFiles = listFiles('scripts/checks', '.js');
if (!checkFiles.length) warnings.push('no hay scripts/checks/*.js todavía');
let ran = 0;
for (const name of checkFiles) {
  const label = 'scripts/checks/' + name;
  try {
    const check = require(path.join(root, 'scripts', 'checks', name));
    assert.strictEqual(typeof check, 'function', 'tiene que exportar module.exports = function (assert, ctx)');
    check(assert, ctx);
    ran += 1;
  } catch (error) {
    if (error instanceof MissingModule) warnings.push(label + ' omitido: ' + error.message);
    else fail(label, error);
  }
}

// --- 3. workflows/*.json ---
const ALLOWED_CREDENTIALS = {
  postgres: ['PostgreSQL'],
  httpHeaderAuth: ['RAG API Key', 'LLM API', 'Instagram Token', 'Instagram API Key'],
  telegramApi: ['Telegram Instagram'],
};
const SECRET_PATTERNS = [
  [/EAA[A-Za-z0-9]{20,}/, 'token de Meta (EAA…)'],
  [/Bearer\s+[A-Za-z0-9._~+\/=-]{20,}/, 'Bearer con token real'],
  [/\b[0-9a-fA-F]{40,}\b/, 'cadena hexadecimal de 40 o más caracteres'],
  [/\b\d{8,10}:[A-Za-z0-9_-]{35}\b/, 'token de bot de Telegram'],
  [/\bsk-[A-Za-z0-9_-]{20,}/, 'clave tipo sk-…'],
  [/github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}/, 'token de GitHub'],
];

function scanSecrets(label, text) {
  for (const [pattern, what] of SECRET_PATTERNS) {
    const match = pattern.exec(text);
    assert.ok(!match, label + ': parece contener un secreto (' + what + '): ' + (match ? match[0].slice(0, 8) + '…' : ''));
  }
}

const workflowFiles = listFiles('workflows', '.json');
if (!workflowFiles.length) {
  warnings.push('workflows/*.json: aún no hay flujos, se saltan las comprobaciones de flujos');
} else {
  for (const file of workflowFiles) {
    guard('workflows/' + file, () => {
      const label = 'workflows/' + file;
      const text = fs.readFileSync(path.join(root, 'workflows', file), 'utf8');
      scanSecrets(label, text);
      const flow = JSON.parse(text);
      assert.ok(Array.isArray(flow.nodes) && flow.nodes.length, label + ': sin nodos');
      const names = flow.nodes.map((item) => item.name);
      assert.strictEqual(names.length, new Set(names).size, label + ': nombres de nodo repetidos');
      const config = flow.nodes.find((item) => item.name === 'Configuración');
      assert.ok(config, label + ': falta el nodo Configuración');
      assert.strictEqual(config.type, 'n8n-nodes-base.set', label + ': Configuración tiene que ser un nodo Set');
      for (const [source, outputs] of Object.entries(flow.connections || {})) {
        assert.ok(names.includes(source), label + ': conexión desde un nodo que no existe: ' + source);
        for (const branch of outputs.main || []) {
          for (const edge of branch) assert.ok(names.includes(edge.node), label + ': conexión hacia un nodo que no existe: ' + edge.node);
        }
      }
      for (const item of flow.nodes) {
        assert.notStrictEqual(item.type, 'n8n-nodes-base.wait', label + ': ' + item.name + ': sin nodos Wait (el estado vive en Postgres)');
        assert.ok(!/\$env|\$vars|process\.env/.test(JSON.stringify(item.parameters || {})), label + ': ' + item.name + ': no puede leer variables de entorno');
        for (const [type, ref] of Object.entries(item.credentials || {})) {
          const allowed = ALLOWED_CREDENTIALS[type];
          assert.ok(allowed, label + ': ' + item.name + ': tipo de credencial no previsto (' + type + ')');
          assert.strictEqual(ref.id, null, label + ': ' + item.name + ': la credencial va por nombre, con id null');
          assert.ok(allowed.includes(ref.name), label + ': ' + item.name + ': credencial «' + ref.name + '» no está en el diseño (' + allowed.join(', ') + ')');
        }
      }
    });
  }
}

// --- test-data: sin secretos ---
for (const file of listFiles('test-data', '.json')) {
  guard('test-data/' + file, () => scanSecrets('test-data/' + file, fs.readFileSync(path.join(root, 'test-data', file), 'utf8')));
}

// --- Nada de «latest» en imágenes ni dependencias ---
for (const dir of ['.', 'workflows', 'runners']) {
  for (const file of fs.readdirSync(path.join(root, dir)).filter((name) => /\.(json|ya?ml|sql|md|example)$|^\.env/.test(name))) {
    const full = path.join(root, dir, file);
    if (!fs.statSync(full).isFile()) continue;
    guard(dir + '/' + file, () => assert.ok(!/[:@]latest\b/.test(fs.readFileSync(full, 'utf8')), 'nada de :latest ni @latest, fija la versión'));
  }
}

// --- Resultado ---
for (const warning of warnings) console.log('aviso: ' + warning);
if (failures.length) {
  console.error('\ncheck FALLA (' + failures.length + '):');
  for (const item of failures) console.error('  - ' + item);
  process.exit(1);
}
console.log('check ok (' + srcFiles.length + ' módulos de src, ' + ran + ' ficheros de pruebas, ' + workflowFiles.length + ' flujos)');
