#!/usr/bin/env python3
"""Genera workflows/*.json a partir de src/ y prompts/. No lleva secretos.

    python3 scripts/build_workflows.py          # escribe los JSON de workflows/
    python3 scripts/build_workflows.py --check  # falla si algún JSON no está al día

Los flujos viven en dos módulos (no se mezclan):
    scripts/wf_comments.py  IG 01 Webhook, IG 02 Comentarios, IG 02b Reintentos, IG 08 Graph API
    scripts/wf_dm.py        IG 03 a IG 07 (mensajes, envío, aprobación, botones, resumen)
Los helpers comunes están en scripts/wf_common.py.

Además de generar, comprueba que al inlinear varios src/*.js (o src/*.py) en un Code
node no se repite ningún nombre de función, constante o clase en el script resultante
(en JavaScript un `const` repetido rompe todo el nodo).
"""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import importlib.util  # noqa: E402

import wf_comments  # noqa: E402

# wf_dm.py lo construye otro paso del trabajo: si todavía no existe se avisa y se sigue.
# Si existe pero falla al importarse, el error se ve (no se esconde detrás del aviso).
if importlib.util.find_spec("wf_dm") is not None:
    import wf_dm  # noqa: E402
else:
    wf_dm = None

# Declaraciones de primer nivel (columna 0) que chocan si dos módulos se inlinean juntos.
JS_DECLARATION = re.compile(r"^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)|^(?:const|let|var|class)\s+([A-Za-z_$][\w$]*)")
PY_DECLARATION = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)|^class\s+([A-Za-z_]\w*)|^([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")


def duplicated_names(script, python):
    pattern = PY_DECLARATION if python else JS_DECLARATION
    seen = {}
    repeated = []
    for line in script.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        name = next(group for group in match.groups() if group)
        if name in seen and name not in repeated:
            repeated.append(name)
        seen[name] = True
    return repeated


def check_inlined_names(file_name, payload):
    problems = []
    for item in payload["nodes"]:
        if item["type"] != "n8n-nodes-base.code":
            continue
        params = item["parameters"]
        python = params.get("language") == "pythonNative"
        script = params.get("pythonCode") if python else params.get("jsCode")
        for name in duplicated_names(script or "", python):
            problems.append(f"{file_name}: nodo «{item['name']}»: «{name}» está declarado más de una vez al inlinear los módulos")
    return problems


def build():
    flows = dict(wf_comments.build_flows())
    if wf_dm is not None:
        flows.update(wf_dm.build_flows())
    else:
        print("aviso: scripts/wf_dm.py todavía no existe: solo se generan IG 01, 02, 02b y 08", file=sys.stderr)
    return flows


def render(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def main():
    check = "--check" in sys.argv[1:]
    folder = ROOT / "workflows"
    flows = build()
    problems = []
    for name, payload in flows.items():
        problems.extend(check_inlined_names(name, payload))
    if problems:
        raise SystemExit("nombres repetidos al inlinear módulos:\n  " + "\n  ".join(problems))
    stale = []
    for name, payload in flows.items():
        path = folder / name
        text = render(payload)
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
            continue
        folder.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(name, len(payload["nodes"]))
    if check:
        if stale:
            raise SystemExit("JSON desactualizados (ejecuta python3 scripts/build_workflows.py): " + ", ".join(stale))
        print("workflows al día")


if __name__ == "__main__":
    main()
