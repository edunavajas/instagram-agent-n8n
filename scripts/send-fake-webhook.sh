#!/usr/bin/env bash
# Manda webhooks falsos de Instagram al flujo «IG 01 - Webhook», firmados como
# lo haría Meta (X-Hub-Signature-256 = HMAC-SHA256 del cuerpo con el secreto de
# la app). Sirve para probar sin tener la app de Meta aprobada.
#
#   scripts/send-fake-webhook.sh <caso|todos> [--url URL]
#
# Casos: comentario-palabra, comentario-sin-palabra, comentario-propio, duplicado,
#        dm-facil, dm-sensible, dm-inyeccion, echo, firma-invalida, rafaga,
#        verificacion-get
#
# Lee IG_WEBHOOK_URL, IG_APP_SECRET e IG_VERIFY_TOKEN del entorno o de .env
# (el entorno gana). Los payloads están en test-data/ (datos inventados).
# En cada ejecución cambia los ids por otros nuevos (menos en «duplicado», que
# manda el mismo cuerpo dos veces), así que se puede repetir sin chocar con la
# deduplicación. El secreto se pasa a openssl por argumento: es una prueba en
# tu máquina, no lo uses con el secreto de producción en un equipo compartido.
#
# Qué comprueba: solo el código HTTP (y el challenge del GET). Que el evento se
# haya procesado o descartado hay que mirarlo en las ejecuciones de n8n o en
# las tablas ig_events / ig_comments / ig_messages / ig_graph_log (DRY_RUN).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/test-data"
ALL_CASES="verificacion-get comentario-palabra comentario-sin-palabra comentario-propio duplicado dm-facil dm-sensible dm-inyeccion echo firma-invalida rafaga"

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

# --- Argumentos ---
CASE=""
URL_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) [ $# -ge 2 ] || usage; URL_ARG="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    -*) echo "Opción desconocida: $1" >&2; usage ;;
    *) [ -z "$CASE" ] || usage; CASE="$1"; shift ;;
  esac
done
[ -n "$CASE" ] || usage

# --- Configuración: entorno primero, .env después. No se hace "source" del .env ---
env_value() {
  local key="$1" current="${!1:-}"
  if [ -n "$current" ]; then printf '%s' "$current"; return; fi
  [ -f "$ROOT/.env" ] || return 0
  local line value
  line="$(grep -E "^${key}=" "$ROOT/.env" | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  printf '%s' "$value"
}
is_placeholder() { case "$1" in ""|"<"*) return 0 ;; *) return 1 ;; esac; }

URL="${URL_ARG:-$(env_value IG_WEBHOOK_URL)}"
SECRET="$(env_value IG_APP_SECRET)"
VERIFY_TOKEN="$(env_value IG_VERIFY_TOKEN)"
if [ -n "$URL_ARG" ]; then :; elif [[ "$URL" == *"<"* ]]; then URL=""; fi

command -v curl >/dev/null || { echo "Falta curl" >&2; exit 2; }
command -v openssl >/dev/null || { echo "Falta openssl" >&2; exit 2; }
[ -n "$URL" ] || { echo "Falta la URL: IG_WEBHOOK_URL en .env o --url" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- Utilidades ---
# Id nuevo en cada ejecución: segundos + aleatorio (15 dígitos).
uid() { printf '%s%05d' "$(date +%s)" "$RANDOM"; }

# Firma del cuerpo EXACTO que se manda (se firma el fichero y se envía ese fichero).
sign() { openssl dgst -sha256 -hmac "$SECRET" -r "$1" | cut -d' ' -f1; }

# Copia un fichero de test-data cambiando un texto por otro nuevo. Salida: ruta.
fresh() {
  local name="$1" from="$2" to="$3" out="$TMP/$1-$RANDOM.json"
  sed "s/${from}/${to}/g" "$DATA/${name}.json" > "$out"
  printf '%s' "$out"
}

# POST firmado de un fichero. Devuelve el código HTTP (000 si no hubo conexión).
post_file() {
  local file="$1" signature="${2:-}"
  [ -n "$signature" ] || signature="sha256=$(sign "$file")"
  curl -sS -o "$TMP/last-body" -w '%{http_code}' --max-time 30 -X POST \
    -H 'Content-Type: application/json' \
    -H "X-Hub-Signature-256: $signature" \
    --data-binary "@$file" "$URL" 2>/dev/null || printf '000'
}

ROWS=()
FAILED=0
# registrar <caso> <esperado> <obtenido> [<nota>]
record() {
  local mark="OK"
  if [ "$2" != "$3" ]; then mark="FALLA"; FAILED=1; fi
  ROWS+=("$1|$2|$3|$mark${4:+  ($4)}")
}

need_secret() {
  if is_placeholder "$SECRET"; then
    echo "Falta IG_APP_SECRET (en .env o en el entorno) para firmar." >&2
    exit 2
  fi
}

# --- Casos ---
case_comentario_palabra() {
  local file; file="$(fresh comentario-palabra 18000000000000001 "18$(uid)")"
  record comentario-palabra 200 "$(post_file "$file")" "debe aparecer una ejecución de IG 02 y una fila en ig_comments"
}
case_comentario_sin_palabra() {
  local file; file="$(fresh comentario-sin-palabra 18000000000000003 "18$(uid)")"
  record comentario-sin-palabra 200 "$(post_file "$file")" "se responde 200 y no se contesta nada"
}
case_comentario_propio() {
  local file; file="$(fresh comentario-propio 18000000000000004 "18$(uid)")"
  record comentario-propio 200 "$(post_file "$file")" "se descarta como own_comment"
}
case_duplicado() {
  local file first second
  file="$(fresh comentario-palabra 18000000000000001 "18$(uid)")"
  first="$(post_file "$file")"
  second="$(post_file "$file")"
  record "duplicado (1.ª vez)" 200 "$first"
  record "duplicado (2.ª vez)" 200 "$second" "en ig_events debe haber UNA sola fila"
}
case_dm() { # <caso> <texto a cambiar>
  local file; file="$(fresh "$1" "$2" "$3$(uid)")"
  record "$1" 200 "$(post_file "$file")"
}
case_echo() {
  local file; file="$(fresh echo aWdfZAG1fakeecho0001 "aWdfZAG1fakeecho$(uid)")"
  record echo 200 "$(post_file "$file")" "se descarta como echo"
}
case_firma_invalida() {
  local file bad
  file="$(fresh comentario-palabra 18000000000000001 "18$(uid)")"
  bad="sha256=$(printf '%064d' 0)"
  record firma-invalida 403 "$(post_file "$file" "$bad")" "no debe procesarse nada"
  record "firma ausente" 403 "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X POST -H 'Content-Type: application/json' --data-binary "@$file" "$URL" 2>/dev/null || printf '000')"
}
case_rafaga() {
  local dir="$TMP/rafaga" start end secs ok=0 code total=50
  mkdir -p "$dir"
  local base; base="$(uid)"
  local i
  for i in $(seq 1 "$total"); do
    sed "s/__ID__/18${base}$(printf '%02d' "$i")/g" "$DATA/rafaga.json" > "$dir/in-$i.json"
  done
  export URL SECRET dir
  start="$(date +%s.%N)"
  # 10 hilos a la vez. Cada hilo firma su cuerpo y guarda el código que recibe.
  # shellcheck disable=SC2016  # las comillas simples son a propósito: expande el hilo hijo
  seq 1 "$total" | xargs -P 10 -I{} bash -c '
    file="$dir/in-{}.json"
    sig="sha256=$(openssl dgst -sha256 -hmac "$SECRET" -r "$file" | cut -d" " -f1)"
    curl -sS -o /dev/null -w "%{http_code}" --max-time 30 -X POST \
      -H "Content-Type: application/json" -H "X-Hub-Signature-256: $sig" \
      --data-binary "@$file" "$URL" > "$dir/code-{}" 2>/dev/null || printf 000 > "$dir/code-{}"
  '
  end="$(date +%s.%N)"
  for i in $(seq 1 "$total"); do
    code="$(cat "$dir/code-$i" 2>/dev/null || printf 000)"
    [ "$code" = "200" ] && ok=$((ok + 1))
  done
  secs="$(awk -v a="$start" -v b="$end" 'BEGIN { printf "%.2f", b - a }')"
  record "rafaga (${total} comentarios, 10 en paralelo)" "${total}x200" "${ok}x200" "${secs} s en total; en ig_comments deben quedar ${total} filas y ninguna repetida"
}
case_verificacion_get() {
  if is_placeholder "$VERIFY_TOKEN"; then
    echo "Falta IG_VERIFY_TOKEN (en .env o en el entorno) para probar la verificación." >&2
    exit 2
  fi
  local challenge="1158201444$RANDOM" code body type
  code="$(curl -sS -G -o "$TMP/get-body" -D "$TMP/get-headers" -w '%{http_code}' --max-time 30 \
    --data-urlencode 'hub.mode=subscribe' \
    --data-urlencode "hub.verify_token=$VERIFY_TOKEN" \
    --data-urlencode "hub.challenge=$challenge" "$URL" 2>/dev/null || printf '000')"
  body="$(cat "$TMP/get-body" 2>/dev/null || true)"
  type="$(grep -i '^content-type:' "$TMP/get-headers" 2>/dev/null | tr -d '\r' | head -n 1 || true)"
  if [ "$code" = "200" ] && [ "$body" = "$challenge" ]; then
    case "$type" in
      *json*) record "verificacion-get (token bueno)" "challenge en texto plano" "challenge dentro de JSON" "$type" ;;
      *) record "verificacion-get (token bueno)" "challenge en texto plano" "challenge en texto plano" ;;
    esac
  else
    record "verificacion-get (token bueno)" "200 + challenge" "$code + ${body:0:30}"
  fi
  code="$(curl -sS -G -o /dev/null -w '%{http_code}' --max-time 30 \
    --data-urlencode 'hub.mode=subscribe' \
    --data-urlencode 'hub.verify_token=token-incorrecto' \
    --data-urlencode "hub.challenge=$challenge" "$URL" 2>/dev/null || printf '000')"
  record "verificacion-get (token malo)" 403 "$code"
}

run_case() {
  case "$1" in
    comentario-palabra) need_secret; case_comentario_palabra ;;
    comentario-sin-palabra) need_secret; case_comentario_sin_palabra ;;
    comentario-propio) need_secret; case_comentario_propio ;;
    duplicado) need_secret; case_duplicado ;;
    dm-facil) need_secret; case_dm dm-facil aWdfZAG1fakefacil0001 aWdfZAG1fakefacil ;;
    dm-sensible) need_secret; case_dm dm-sensible aWdfZAG1fakesensible0001 aWdfZAG1fakesensible ;;
    dm-inyeccion) need_secret; case_dm dm-inyeccion aWdfZAG1fakeinyeccion0001 aWdfZAG1fakeinyeccion ;;
    echo) need_secret; case_echo ;;
    firma-invalida) need_secret; case_firma_invalida ;;
    rafaga) need_secret; case_rafaga ;;
    verificacion-get) case_verificacion_get ;;
    *) echo "Caso desconocido: $1" >&2; usage ;;
  esac
}

if [ "$CASE" = "todos" ]; then
  for c in $ALL_CASES; do run_case "$c"; done
else
  run_case "$CASE"
fi

# --- Tabla «caso · esperado · obtenido» ---
echo
printf '%-44s %-26s %-26s %s\n' "CASO" "ESPERADO" "OBTENIDO" "RESULTADO"
printf '%-44s %-26s %-26s %s\n' "----" "--------" "--------" "---------"
for row in "${ROWS[@]}"; do
  IFS='|' read -r c e o r <<< "$row"
  printf '%-44s %-26s %-26s %s\n' "$c" "$e" "$o" "$r"
done
echo
if [ "$FAILED" -eq 0 ]; then
  echo "Todo como se esperaba. Recuerda: aquí solo se ve el código HTTP; mira las ejecuciones de n8n y las tablas ig_*."
else
  echo "Hay casos que no cuadran." >&2
fi
exit "$FAILED"
