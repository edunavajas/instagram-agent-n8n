"""Helpers comunes para generar workflows/*.json (estilo del repo email-agent-n8n).

Los IDs de nodo son deterministas (uuid5): el mismo código produce el mismo JSON.
No lleva secretos: las credenciales van por nombre y sin id.
"""

import json
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NS = uuid.UUID("b7e41d6a-52c3-4f08-9a1e-3d5c7f2b8a64")

# Las credenciales van por nombre, sin id. Si en n8n ya existe una credencial
# con ese nombre y ese tipo, n8n la asigna sola al importar.
CREDENTIALS = {
    "pg": ("postgres", "PostgreSQL"),
    "rag": ("httpHeaderAuth", "RAG API Key"),
    "llm": ("httpHeaderAuth", "LLM API"),
    "igtoken": ("httpHeaderAuth", "Instagram Token"),
    "igkey": ("httpHeaderAuth", "Instagram API Key"),
    "telegram": ("telegramApi", "Telegram Instagram"),
}


def nid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


def cred(key):
    type_, name = CREDENTIALS[key]
    return {type_: {"id": None, "name": name}}


def src(*names: str) -> str:
    chunks = []
    for name in names:
        text = (ROOT / "src" / name).read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if not line.startswith("module.exports")]
        chunks.append("\n".join(lines).strip())
    return "\n\n".join(chunks)


def node(name, type_, version, x, y, parameters, notes="", extra=None, credentials=None):
    item = {
        "parameters": parameters,
        "id": nid(name + type_),
        "name": name,
        "type": type_,
        "typeVersion": version,
        "position": [x, y],
    }
    if credentials:
        item["credentials"] = cred(credentials)
    if notes:
        item["notes"] = notes
    if extra:
        item.update(extra)
    return item


def code(name, x, y, body, files=(), each=True, notes="", extra=None):
    prefix = src(*files)
    script = f"{prefix}\n\n{body.strip()}\n" if prefix else body.strip() + "\n"
    parameters = {"jsCode": script}
    if each:
        parameters["mode"] = "runOnceForEachItem"
    return node(name, "n8n-nodes-base.code", 2, x, y, parameters, notes, extra)




def py_code(name, x, y, body, files=(), each=True, notes="", extra=None):
    """Code node de Python (runner nativo). Antepone ficheros de src/ (p. ej. signature.py)."""
    chunks = [(ROOT / "src" / f).read_text(encoding="utf-8").strip() for f in files]
    script = "\n\n\n".join(chunks + [body.strip()]) + "\n"
    parameters = {"language": "pythonNative", "pythonCode": script}
    if each:
        parameters["mode"] = "runOnceForEachItem"
    return node(name, "n8n-nodes-base.code", 2, x, y, parameters, notes, extra)


def postgres(name, x, y, query, replacement, notes="", extra=None):
    # Ojo: con retryOnFail, n8n reintenta el nodo si un item de salida trae la
    # clave "error". Ninguna consulta devuelve una columna con ese nombre.
    if re.search(r"\bas\s+\"?error\"?\b", query, re.IGNORECASE):
        raise SystemExit(f"{name}: no devuelvas una columna 'error' (n8n reintenta el nodo)")
    options = {"queryBatching": "independently"}
    if replacement:
        options["queryReplacement"] = replacement
    settings = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}
    if extra:
        settings.update(extra)
    return node(
        name,
        "n8n-nodes-base.postgres",
        2.5,
        x,
        y,
        {"operation": "executeQuery", "query": query, "options": options},
        notes,
        settings,
        "pg",
    )


def http_json(name, x, y, url, body_expr, credential, notes="", text=False, headers=None, timeout=120000, tries=3):
    response = {"response": {"responseFormat": "text", "outputPropertyName": "rag_raw"}} if text else {"response": {"responseFormat": "json"}}
    parameters = {
        "method": "POST",
        "url": url,
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": body_expr,
        "options": {"timeout": timeout, "response": response},
    }
    if headers:
        parameters["sendHeaders"] = True
        parameters["headerParameters"] = {"parameters": [{"name": key, "value": value} for key, value in headers]}
    return node(
        name,
        "n8n-nodes-base.httpRequest",
        4.2,
        x,
        y,
        parameters,
        notes,
        {"retryOnFail": True, "maxTries": tries, "waitBetweenTries": 2000, "onError": "continueErrorOutput"},
        credential,
    )


def keyboard(*buttons):
    return {"rows": [{"row": {"buttons": [{"text": text, "additionalFields": {"callback_data": data}} for text, data in buttons]}}]}


def telegram(name, x, y, chat_expr, text_expr, notes="", buttons=None, force_reply=False, reply_to=None, on_error="continueErrorOutput"):
    """sendMessage siempre en HTML (el texto ya va escapado). Sin HTML, n8n usa
    Markdown y un guion bajo del correo rompe el mensaje."""
    additional = {"parse_mode": "HTML", "appendAttribution": False, "disable_web_page_preview": True}
    if reply_to:
        additional["reply_to_message_id"] = reply_to
    parameters = {
        "resource": "message",
        "operation": "sendMessage",
        "chatId": chat_expr,
        "text": text_expr,
        "replyMarkup": "none",
        "additionalFields": additional,
    }
    if buttons:
        parameters["replyMarkup"] = "inlineKeyboard"
        parameters["inlineKeyboard"] = keyboard(*buttons)
    if force_reply:
        parameters["replyMarkup"] = "forceReply"
        parameters["forceReply"] = {"force_reply": True}
    return node(name, "n8n-nodes-base.telegram", 1.2, x, y, parameters, notes, {"onError": on_error}, "telegram")


def telegram_edit(name, x, y, chat_expr, message_expr, text_expr, notes="", buttons=None):
    """editMessageText en HTML. Sin botones va un teclado vacío ({"inline_keyboard": []}):
    Telegram quita los que había. Si la edición falla (mensaje igual, borrado,
    demasiado viejo), el flujo sigue: el envío del correo no depende de ella."""
    parameters = {
        "resource": "message",
        "operation": "editMessageText",
        "messageType": "message",
        "chatId": chat_expr,
        "messageId": message_expr,
        "text": text_expr,
        "replyMarkup": "inlineKeyboard",
        "inlineKeyboard": keyboard(*buttons) if buttons else {},
        "additionalFields": {"parse_mode": "HTML", "disable_web_page_preview": True},
    }
    return node(name, "n8n-nodes-base.telegram", 1.2, x, y, parameters, notes, {"onError": "continueRegularOutput"}, "telegram")


def switch_if(name, x, y, left, right, notes=""):
    return node(
        name,
        "n8n-nodes-base.if",
        2.2,
        x,
        y,
        {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "leftValue": left,
                    "rightValue": right,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        notes,
    )


def bool_if(name, x, y, left, notes=""):
    return node(
        name,
        "n8n-nodes-base.if",
        2.2,
        x,
        y,
        {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "leftValue": left,
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        notes,
    )


def sticky(name, x, y, content, width=560, height=240):
    return node(
        name,
        "n8n-nodes-base.stickyNote",
        1,
        x,
        y,
        {"content": content, "height": height, "width": width, "color": 5},
    )


PROMPT = (ROOT / "prompts" / "clasificador.md").read_text(encoding="utf-8").strip()

PROMPT = (ROOT / "prompts" / "clasificador.md").read_text(encoding="utf-8").strip()

# Todos los campos que pueden aparecer en un nodo «Configuración», con su valor por
# defecto y su tipo. Cada flujo elige los que usa. dry_run es un string "true"/"false".
FIELDS = {
    "verify_token": ("cambia-esto", "string"),
    "ig_account_id": ("cambia-esto", "string"),
    "ig_username": ("cambia-esto", "string"),
    "dry_run": ("true", "string"),
    "graph_base": ("https://graph.facebook.com", "string"),
    "graph_version": ("v27.0", "string"),
    "max_attempts": (6, "number"),
    "window_days": (7, "number"),
    "window_hours": (24, "number"),
    "loop_minutes": (10, "number"),
    "confidence_threshold": (0.75, "number"),
    "min_rag_score": (0.45, "number"),
    "approval_hours": (24, "number"),
    "retention_days": (90, "number"),
    "sensitive_topics": ("precio,precios,cuanto cuesta,cuánto cuesta,tarifa,tarifas,presupuesto,descuento,oferta,contrato,legal,devolucion,devolución,reembolso,garantia,garantía,factura,dni,iban,tarjeta,telefono,teléfono,direccion,dirección,datos personales,contraseña,password", "string"),
    "llm_base_url": ("https://api.deepseek.com", "string"),
    "llm_model": ("deepseek-flash", "string"),
    # n8n se llama a sí mismo: localhost:5678 es el propio contenedor de n8n.
    "rag_url": ("http://localhost:5678/webhook/rag-search", "string"),
    # repo = FAQ y documentos de soporte indexados desde repositorios (ver README).
    "rag_source_type": ("repo", "string"),
    "n8n_url": ("http://localhost:5678", "string"),
    "telegram_chat_id": ("cambia-esto", "string"),
    "telegram_user_ids": ("", "string"),
    "prompt": (PROMPT, "string"),
}


def config_node(x, y, keys):
    assignments = []
    for key in keys:
        value, typ = FIELDS[key]
        assignments.append({"id": nid("cfg-" + key), "name": key, "value": value, "type": typ})
    return node(
        "Configuración",
        "n8n-nodes-base.set",
        3.4,
        x,
        y,
        {
            "mode": "manual",
            "duplicateItem": False,
            "includeOtherFields": True,
            "assignments": {"assignments": assignments},
            "options": {},
        },
        "Valores que se ven en cámara. El .env no entra aquí: Coolify bloquea $env. Los secretos NO van aquí.",
    )


def webhook(name, path, x, y, notes, credential="igkey", method="POST", response_mode="onReceived"):
    """Webhook interno protegido con la credencial «Instagram API Key» (cabecera
    X-IG-API-Key). Responde nada más recibir: el trabajo sigue en segundo plano.
    El webhook público de Meta (flujo 01) NO usa esta función: va sin cabecera."""
    return node(
        name,
        "n8n-nodes-base.webhook",
        2.1,
        x,
        y,
        {
            "httpMethod": method,
            "path": path,
            "authentication": "headerAuth",
            "responseMode": response_mode,
            "options": {},
        },
        notes,
        {"webhookId": nid("wh-" + path)},
        credential,
    )


def link(connections, source, target, output=0, input_index=0):
    connections.setdefault(source, {"main": []})
    while len(connections[source]["main"]) <= output:
        connections[source]["main"].append([])
    connections[source]["main"][output].append({"node": target, "type": "main", "index": input_index})


def pack(name, nodes, connections):
    names = [item["name"] for item in nodes]
    if len(names) != len(set(names)):
        raise SystemExit(f"{name}: nombres de nodo repetidos")
    for source, outputs in connections.items():
        if source not in names:
            raise SystemExit(f"{name}: origen desconocido {source}")
        for branch in outputs["main"]:
            for edge in branch:
                if edge["node"] not in names:
                    raise SystemExit(f"{name}: destino desconocido {edge['node']}")
    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
    }


UUID_RE = r"/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i"
