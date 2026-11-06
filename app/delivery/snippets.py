"""Copy-paste code built from the exact request we sent. It never contains our key."""

from __future__ import annotations

import json
import shlex
from typing import Any

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"


def _scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, str) else repr(value)


def _literal(value: Any, indent: int = 0) -> str:
    """Formats a body as readable Python. JSON won't do: it would write true where Python needs True."""
    pad = " " * (indent + 4)
    close = " " * indent
    if isinstance(value, dict) and value:
        items = "".join(f"{pad}{_scalar(k)}: {_literal(v, indent + 4)},\n" for k, v in value.items())
        return "{\n" + items + close + "}"
    if isinstance(value, list) and value:
        inline = "[" + ", ".join(_literal(v) for v in value) + "]"
        if len(inline) <= 60 and not any(isinstance(v, (dict, list)) for v in value):
            return inline
        items = "".join(f"{pad}{_literal(v, indent + 4)},\n" for v in value)
        return "[\n" + items + close + "]"
    return _scalar(value)


def curl(endpoint: str, body: dict[str, Any]) -> str:
    payload = json.dumps(body, indent=2)
    return (
        f"curl -X POST {BASE_URL}{endpoint} \\\n"
        '  -H "Authorization: Bearer $CRUSTDATA_API_KEY" \\\n'
        f'  -H "x-api-version: {API_VERSION}" \\\n'
        '  -H "content-type: application/json" \\\n'
        f"  -d {shlex.quote(payload)}"
    )


def python(endpoint: str, body: dict[str, Any]) -> str:
    payload = _literal(body)
    return (
        "import os\n\nimport requests\n\n"
        f"body = {payload}\n\n"
        "response = requests.post(\n"
        f'    "{BASE_URL}{endpoint}",\n'
        "    headers={\n"
        '        "Authorization": f"Bearer {os.environ[\'CRUSTDATA_API_KEY\']}",\n'
        f'        "x-api-version": "{API_VERSION}",\n'
        '        "content-type": "application/json",\n'
        "    },\n"
        "    json=body,\n"
        ")\n"
        "response.raise_for_status()\n"
        "print(response.json())\n"
    )
