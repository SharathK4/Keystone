"""Generate the frontend's API types from the backend's OpenAPI schema.

    PYTHONPATH=src python scripts/gen_frontend_types.py

Writes ``frontend/src/api/openapi.json`` (the schema, for reference and tests)
and ``frontend/src/api/schema.ts`` (one exported type per component schema).
The frontend imports response types only from ``schema.ts``, so a field renamed
in the backend fails the frontend build instead of rendering ``undefined``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "src" / "api"
IDENT = re.compile(r"[^A-Za-z0-9_]")


def name_of(ref: str) -> str:
    return IDENT.sub("_", ref.rsplit("/", 1)[-1])


def ts(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return name_of(schema["$ref"])
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = []
            for s in schema[key]:
                t = ts(s)
                if t not in parts:
                    parts.append(t)
            return " | ".join(parts)
    if "allOf" in schema:
        return " & ".join(ts(s) for s in schema["allOf"])
    kind = schema.get("type")
    if kind == "null":
        return "null"
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "array":
        item = ts(schema.get("items", {}))
        return f"({item})[]" if "|" in item else f"{item}[]"
    if kind == "object" or "properties" in schema:
        if "properties" in schema:
            return "{ " + "; ".join(field(k, v, k in schema.get("required", []))
                                    for k, v in schema["properties"].items()) + " }"
        extra = schema.get("additionalProperties")
        value = ts(extra) if isinstance(extra, dict) and extra else "unknown"
        return f"Record<string, {value}>"
    return "unknown"


def field(key: str, schema: dict[str, Any], required: bool) -> str:
    safe = key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else json.dumps(key)
    return f"{safe}{'' if required else '?'}: {ts(schema)}"


def declaration(name: str, schema: dict[str, Any]) -> str:
    doc = schema.get("description", "").strip().splitlines()
    head = f"/** {doc[0]} */\n" if doc else ""
    if "properties" in schema:
        body = "\n".join(f"  {field(k, v, k in schema.get('required', []))}"
                         for k, v in schema["properties"].items())
        return f"{head}export interface {name} {{\n{body}\n}}"
    return f"{head}export type {name} = {ts(schema)}"


def main() -> None:
    from lce.api.app import create_app

    spec = create_app().openapi()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "openapi.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8", newline="\n")
    schemas = spec.get("components", {}).get("schemas", {})
    blocks = [declaration(name_of(n), s) for n, s in sorted(schemas.items())]
    header = ("// Generated from the backend's OpenAPI schema by scripts/gen_frontend_types.py.\n"
              "// Do not edit by hand; re-run the script after a backend change.\n\n")
    (OUT / "schema.ts").write_text(header + "\n\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(blocks)} types")


if __name__ == "__main__":
    main()
