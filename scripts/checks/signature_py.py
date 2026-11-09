#!/usr/bin/env python3
"""Pruebas de src/signature.py (la verificación que corre en el runner de Python).

    python3 scripts/checks/signature_py.py

Mismos vectores que scripts/checks/signature.js (fijados con openssl).
"""
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("signature", ROOT / "src" / "signature.py")
sig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sig)
V = sig.verify_signature

SECRET = "secreto-de-prueba-no-real"
BODY = ('{"object":"instagram","entry":[{"id":"17841400000000001","time":1762700000,"changes":[{"field":"comments",'
        '"value":{"from":{"id":"1234567890123456","username":"usuario_demo"},"comment_id":"18000000000000001",'
        '"text":"GUIA ¡Qué guay! 🚀","media":{"id":"18050000000000001","media_product_type":"REELS"}}}]}]}')
BODY_SIG = "56f4b018c80c2678ac49369e1df310f624d8fde52c839667e73c999b6955c9de"

casos = [
    (V("abc", "sha256=9c196e32dc0175f86f4b1cb89289d6619de6bee699e4c378e68309ed97a1a6ab", "key"), True, None),
    (V(b"what do ya want for nothing?", "sha256=5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843", "Jefe"), True, None),
    (V(BODY.encode(), "sha256=" + BODY_SIG, SECRET), True, None),
    (V(BODY.encode(), "SHA256=" + BODY_SIG.upper(), SECRET), True, None),
    (V(BODY.encode(), ["sha256=" + BODY_SIG], SECRET), True, None),
    (V(BODY.encode(), "sha256=" + BODY_SIG[:-1] + "0", SECRET), False, "mismatch"),
    (V(BODY.encode() + b" ", "sha256=" + BODY_SIG, SECRET), False, "mismatch"),
    (V(BODY.encode(), "sha256=" + BODY_SIG[:62], SECRET), False, "bad_length"),
    (V(BODY.encode(), "sha256=" + BODY_SIG[:63] + "z", SECRET), False, "malformed_header"),
    (V(BODY.encode(), "sha1=" + BODY_SIG, SECRET), False, "bad_prefix"),
    (V(BODY.encode(), None, SECRET), False, "missing_header"),
    (V(BODY.encode(), "sha256=" + BODY_SIG, ""), False, "missing_secret"),
    (V(b"", "sha256=" + BODY_SIG, SECRET), False, "empty_body"),
    (V(BODY.encode("latin-1", "replace"), "sha256=" + BODY_SIG, SECRET), False, "mismatch"),
    # re-serializar el JSON (lo que haría json.dumps(body)) rompe la firma
    (V(json.dumps(json.loads(BODY)).encode(), "sha256=" + BODY_SIG, SECRET), False, "mismatch"),
]
fallos = 0
for i, (got, ok, reason) in enumerate(casos):
    if got.get("ok") is not ok or (reason and got.get("reason") != reason):
        fallos += 1
        print(f"FALLO caso {i}: {got} (esperado ok={ok} reason={reason})")

os.environ["IG_APP_SECRET"] = SECRET
item = {"raw_b64": base64.b64encode(BODY.encode()).decode(), "signature": "sha256=" + BODY_SIG}
if sig.verify_item(item) != {"ok": True, "reason": None}:
    fallos += 1
    print("FALLO verify_item")
if sig.verify_item({"raw_b64": "%%%", "signature": "sha256=" + BODY_SIG})["ok"]:
    fallos += 1
    print("FALLO verify_item base64 roto")
print("signature.py:", "OK" if not fallos else f"{fallos} fallos", f"({len(casos) + 2} casos)")
sys.exit(1 if fallos else 0)
