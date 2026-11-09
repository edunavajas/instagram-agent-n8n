# Verificación de X-Hub-Signature-256 en el runner de PYTHON de n8n.
#
# Por qué Python y no src/signature.js en el Code node: el App Secret vive como
# variable IG_APP_SECRET del contenedor task-runners. El runner de Python la ve
# con os.environ (N8N_BLOCK_RUNNER_ENV_ACCESS=false y IG_APP_SECRET en
# allowed-env, como SMTP_* del ep3). El runner de JavaScript no tiene forma de
# leer variables sin permitir el módulo 'process' a TODOS los Code nodes JS
# (ver docs/spike-firma.md). src/signature.js hace lo mismo y es el que usan
# las pruebas y el GET de verificación (que no necesita el secreto).
#
# Mismos motivos de rechazo que signature.js. No lanza excepciones.
# Solo usa base64, hashlib, hmac y os (N8N_RUNNERS_STDLIB_ALLOW). Nada de
# type(), getattr() ni __name__: el sandbox del runner los bloquea.
import base64
import hashlib
import hmac
import os

SIGNATURE_PREFIX = "sha256="
HEX_DIGITS = "0123456789abcdef"


def verify_signature(raw, header, app_secret):
    try:
        if not isinstance(app_secret, str) or not app_secret:
            return {"ok": False, "reason": "missing_secret"}
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) == 0:
            return {"ok": False, "reason": "empty_body"}
        if isinstance(header, list):
            header = header[0] if len(header) == 1 else None
        if not isinstance(header, str) or not header.strip():
            return {"ok": False, "reason": "missing_header"}
        value = header.strip()
        if value[: len(SIGNATURE_PREFIX)].lower() != SIGNATURE_PREFIX:
            return {"ok": False, "reason": "bad_prefix"}
        received = value[len(SIGNATURE_PREFIX):].lower()
        if not received or any(c not in HEX_DIGITS for c in received):
            return {"ok": False, "reason": "malformed_header"}
        if len(received) != 64:
            return {"ok": False, "reason": "bad_length"}
        expected = hmac.new(app_secret.encode("utf-8"), bytes(raw), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return {"ok": False, "reason": "mismatch"}
        return {"ok": True, "reason": None}
    except Exception:
        return {"ok": False, "reason": "error"}


def verify_item(item_json):
    """item_json = salida del Code node JS «Cuerpo crudo»: {raw_b64, signature}."""
    try:
        raw = base64.b64decode(item_json.get("raw_b64") or "", validate=True)
    except Exception:
        return {"ok": False, "reason": "empty_body"}
    return verify_signature(raw, item_json.get("signature"), os.environ.get("IG_APP_SECRET", ""))
