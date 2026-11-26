"""Deterministic offline OpenAPI exporter and Angular 19 TypeScript client generator (T-009).

Usage (from ``backend/``):
    python -m app.contracts.codegen          # export backend/openapi.json and regenerate client
    python -m app.contracts.codegen --check  # exit non-zero if committed artifacts have drifted
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.contracts.openapi_app import export_openapi_schema

_HTTP_METHODS_ORDER: tuple[str, ...] = ("get", "post", "put", "patch", "delete")
_GENERATED_HEADER = (
    "// AUTO-GENERATED FILE — DO NOT EDIT DIRECTLY.\n"
    "// Regenerate via: cd backend && .venv/bin/python -m app.contracts.codegen\n"
)


@dataclass(frozen=True, slots=True)
class GeneratedArtifacts:
    """In-memory deterministic outputs produced from the frozen contract schema."""

    openapi_json: str
    models_ts: str
    service_ts: str
    index_ts: str


def default_repo_root() -> Path:
    """Return the repository root containing ``backend/`` and ``frontend/``."""
    return Path(__file__).resolve().parents[3]


def render_openapi_json(schema: dict[str, Any]) -> str:
    """Serialize an OpenAPI dictionary deterministically with sorted keys and trailing newline."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _ts_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return "unknown"


def _is_valid_ts_ident(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name))


def _format_prop_name(name: str) -> str:
    return name if _is_valid_ts_ident(name) else _ts_literal(name)


def _needs_array_parens(ts_type: str) -> bool:
    if ts_type.startswith("[") and ts_type.endswith("]"):
        return False
    return "|" in ts_type or "&" in ts_type or " " in ts_type


def _schema_to_ts(schema: dict[str, Any]) -> str:
    """Convert an OpenAPI 3.1 / JSON Schema fragment to strict, ESLint-clean TypeScript."""
    if "$ref" in schema:
        ref = str(schema["$ref"])
        return ref.rsplit("/", maxsplit=1)[-1]

    if "const" in schema:
        return _ts_literal(schema["const"])

    if "enum" in schema and isinstance(schema["enum"], list):
        return " | ".join(_ts_literal(item) for item in schema["enum"])

    for combiner in ("anyOf", "oneOf"):
        if combiner in schema and isinstance(schema[combiner], list):
            parts: list[str] = []
            has_null = False
            for branch in schema[combiner]:
                if isinstance(branch, dict):
                    ts_part = _schema_to_ts(branch)
                    if ts_part == "null":
                        has_null = True
                    elif ts_part not in parts:
                        parts.append(ts_part)
            if has_null:
                parts.append("null")
            return " | ".join(parts) if parts else "unknown"

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        parts = []
        has_null = False
        for item_type in schema_type:
            if item_type == "null":
                has_null = True
            else:
                sub = dict(schema)
                sub["type"] = item_type
                ts_part = _schema_to_ts(sub)
                if ts_part not in parts:
                    parts.append(ts_part)
        if has_null:
            parts.append("null")
        return " | ".join(parts) if parts else "unknown"

    if schema_type == "string":
        return "string"
    if schema_type in ("integer", "number"):
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"

    if schema_type == "array":
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list) and prefix_items:
            tuple_members = [
                _schema_to_ts(item) if isinstance(item, dict) else "unknown"
                for item in prefix_items
            ]
            return f"[{', '.join(tuple_members)}]"
        items = schema.get("items")
        if isinstance(items, dict) and items:
            item_ts = _schema_to_ts(items)
            if _needs_array_parens(item_ts):
                return f"({item_ts})[]"
            return f"{item_ts}[]"
        return "unknown[]"

    if schema_type == "object" or "properties" in schema or "additionalProperties" in schema:
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            required = set(schema.get("required", []))
            fields: list[str] = []
            for prop_name in sorted(props):
                prop_schema = props[prop_name]
                prop_ts = _schema_to_ts(prop_schema) if isinstance(prop_schema, dict) else "unknown"
                optional_marker = "" if prop_name in required else "?"
                fields.append(f"{_format_prop_name(prop_name)}{optional_marker}: {prop_ts}")
            return "{ " + "; ".join(fields) + " }"
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            val_ts = _schema_to_ts(additional)
            return f"Record<string, {val_ts}>"
        return "Record<string, unknown>"

    return "unknown"


def generate_models_ts(schema: dict[str, Any]) -> str:
    """Generate ``frontend/src/app/api-client/models.ts`` from OpenAPI components.schemas."""
    components_schemas: dict[str, Any] = schema.get("components", {}).get("schemas", {})
    lines: list[str] = [_GENERATED_HEADER]

    for name in sorted(components_schemas):
        definition = components_schemas[name]
        if not isinstance(definition, dict):
            continue

        is_object = (
            definition.get("type") == "object"
            and "enum" not in definition
            and "const" not in definition
            and "anyOf" not in definition
            and "oneOf" not in definition
        )
        if is_object:
            props = definition.get("properties", {})
            required = set(definition.get("required", []))
            lines.append(f"export interface {name} {{")
            if isinstance(props, dict):
                for prop_name in props:
                    prop_schema = props[prop_name]
                    prop_ts = (
                        _schema_to_ts(prop_schema) if isinstance(prop_schema, dict) else "unknown"
                    )
                    optional_marker = "" if prop_name in required else "?"
                    lines.append(f"  {_format_prop_name(prop_name)}{optional_marker}: {prop_ts};")
            lines.append("}")
            lines.append("")
        else:
            ts_type = _schema_to_ts(definition)
            lines.append(f"export type {name} = {ts_type};")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _pascal_case(name: str) -> str:
    return name[0].upper() + name[1:] if name else name


def _camel_case_header(header_name: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", header_name)
    cleaned = [p for p in parts if p]
    if not cleaned:
        return "headerValue"
    return cleaned[0].lower() + "".join(part.capitalize() for part in cleaned[1:])


def _extract_success_response_type(operation: dict[str, Any]) -> str:
    responses = operation.get("responses", {})
    if not isinstance(responses, dict):
        return "void"
    for status_key in sorted(responses):
        if not str(status_key).startswith("2"):
            continue
        resp_obj = responses[status_key]
        if not isinstance(resp_obj, dict):
            continue
        content = resp_obj.get("content", {})
        if not isinstance(content, dict):
            continue
        json_content = content.get("application/json", {})
        if isinstance(json_content, dict) and isinstance(json_content.get("schema"), dict):
            return _schema_to_ts(json_content["schema"])
    return "void"


def _collect_referenced_model_names(ts_expr: str, known_models: set[str]) -> set[str]:
    tokens = set(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", ts_expr))
    return tokens & known_models


def generate_service_ts(schema: dict[str, Any]) -> tuple[str, list[str]]:
    """Generate ``frontend/src/app/api-client/api-client.service.ts`` and exported type names."""
    components_schemas = set(schema.get("components", {}).get("schemas", {}).keys())
    paths: dict[str, Any] = schema.get("paths", {})

    imported_models: set[str] = set()
    param_interfaces: list[str] = []
    exported_types: list[str] = []
    methods: list[str] = []
    uses_http_headers = False
    uses_http_params = False
    uses_http_response = False

    # Collect operations sorted deterministically by (path, method order).
    operations: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted(paths):
        path_item = paths[path]
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS_ORDER:
            op = path_item.get(method)
            if isinstance(op, dict):
                operations.append((path, method, op))

    for path, method, op in operations:
        op_id = str(op["operationId"])
        response_type = _extract_success_response_type(op)
        imported_models |= _collect_referenced_model_names(response_type, components_schemas)

        raw_params = op.get("parameters", [])
        path_params: list[dict[str, Any]] = []
        query_params: list[dict[str, Any]] = []
        header_params: list[dict[str, Any]] = []
        for param in raw_params:
            if not isinstance(param, dict):
                continue
            location = param.get("in")
            if location == "path":
                path_params.append(param)
            elif location == "query":
                query_params.append(param)
            elif location == "header":
                header_params.append(param)

        request_body = op.get("requestBody")
        is_multipart = False
        body_type: str | None = None
        body_required = False
        if isinstance(request_body, dict):
            body_required = bool(request_body.get("required", False))
            content = request_body.get("content", {})
            if isinstance(content, dict):
                if "multipart/form-data" in content:
                    is_multipart = True
                elif "application/json" in content:
                    json_schema = content["application/json"].get("schema", {})
                    if isinstance(json_schema, dict):
                        body_type = _schema_to_ts(json_schema)
                        imported_models |= _collect_referenced_model_names(
                            body_type, components_schemas
                        )

        has_if_none_match = any(
            str(h.get("name", "")).lower() == "if-none-match" for h in header_params
        )

        # Build URL expression.
        url_expr = f"'{path}'"
        if path_params:
            interpolated = path
            for param in path_params:
                pname = str(param["name"])
                interpolated = interpolated.replace(
                    f"{{{pname}}}",
                    f"${{encodeURIComponent(String({pname}))}}",
                )
            url_expr = f"`{interpolated}`"

        # Build query parameter interface if needed.
        query_interface_name: str | None = None
        query_has_required = False
        if query_params:
            uses_http_params = True
            query_interface_name = f"{_pascal_case(op_id)}Params"
            exported_types.append(query_interface_name)
            iface_lines = [f"export interface {query_interface_name} {{"]
            for qparam in query_params:
                qname = str(qparam["name"])
                qreq = bool(qparam.get("required", False))
                if qreq:
                    query_has_required = True
                qschema = qparam.get("schema", {})
                qtype = _schema_to_ts(qschema) if isinstance(qschema, dict) else "string"
                imported_models |= _collect_referenced_model_names(qtype, components_schemas)
                opt = "" if qreq else "?"
                iface_lines.append(f"  {_format_prop_name(qname)}{opt}: {qtype};")
            iface_lines.append("}")
            param_interfaces.append("\n".join(iface_lines))

        # Special handling for conditional ETag GET endpoints (If-None-Match + observe: 'response').
        if has_if_none_match and method == "get":
            uses_http_headers = True
            uses_http_response = True
            method_lines = [
                f"  {op_id}(",
                "    options?: ConditionalGetBodyOptions,",
                f"  ): Observable<{response_type}>;",
                f"  {op_id}(",
                "    options: ConditionalGetResponseOptions,",
                f"  ): Observable<HttpResponse<{response_type}>>;",
                f"  {op_id}(",
                "    options?: ConditionalGetBodyOptions | ConditionalGetResponseOptions,",
                f"  ): Observable<{response_type} | HttpResponse<{response_type}>> {{",
                "    let headers = new HttpHeaders();",
                "    if (options?.ifNoneMatch !== undefined) {",
                "      headers = headers.set('If-None-Match', options.ifNoneMatch);",
                "    }",
                "    if (options?.observe === 'response') {",
                f"      return this.http.get<{response_type}>({url_expr}, {{",
                "        headers,",
                "        observe: 'response',",
                "        withCredentials: true,",
                "      });",
                "    }",
                f"    return this.http.get<{response_type}>({url_expr}, {{",
                "      headers,",
                "      withCredentials: true,",
                "    });",
                "  }",
            ]
            methods.append("\n".join(method_lines))
            continue

        # Standard method arguments.
        args: list[str] = []
        for param in path_params:
            pname = str(param["name"])
            pschema = param.get("schema", {})
            ptype = _schema_to_ts(pschema) if isinstance(pschema, dict) else "string"
            imported_models |= _collect_referenced_model_names(ptype, components_schemas)
            args.append(f"{pname}: {ptype}")

        if is_multipart:
            args.append("file: Blob | File")
            args.append("filename?: string")
        elif body_type is not None:
            if body_required:
                args.append(f"body: {body_type}")
            else:
                args.append(f"body?: {body_type}")

        if query_interface_name is not None:
            q_opt = "" if query_has_required else "?"
            args.append(f"params{q_opt}: {query_interface_name}")

        non_conditional_headers: list[tuple[str, str, bool]] = []
        for hparam in header_params:
            hname = str(hparam["name"])
            harg = _camel_case_header(hname)
            hreq = bool(hparam.get("required", False))
            non_conditional_headers.append((hname, harg, hreq))
            uses_http_headers = True
            args.append(f"{harg}: string" if hreq else f"{harg}?: string")

        sig_args = ", ".join(args)
        body_lines: list[str] = [f"  {op_id}({sig_args}): Observable<{response_type}> {{"]

        if is_multipart:
            body_lines.append("    const formData = new FormData();")
            body_lines.append("    if (filename !== undefined) {")
            body_lines.append("      formData.append('file', file, filename);")
            body_lines.append("    } else {")
            body_lines.append("      formData.append('file', file);")
            body_lines.append("    }")

        if query_params:
            body_lines.append("    let httpParams = new HttpParams();")
            for qparam in query_params:
                qname = str(qparam["name"])
                qreq = bool(qparam.get("required", False))
                accessor = f"params.{qname}" if query_has_required else f"params?.{qname}"
                if qreq:
                    body_lines.append(
                        f"    httpParams = httpParams.set('{qname}', String({accessor}));"
                    )
                else:
                    body_lines.append(
                        f"    if ({accessor} !== undefined && {accessor} !== null) {{"
                    )
                    body_lines.append(
                        f"      httpParams = httpParams.set('{qname}', String({accessor}));"
                    )
                    body_lines.append("    }")

        if non_conditional_headers:
            body_lines.append("    let headers = new HttpHeaders();")
            for hname, harg, hreq in non_conditional_headers:
                if hreq:
                    body_lines.append(f"    headers = headers.set('{hname}', {harg});")
                else:
                    body_lines.append(f"    if ({harg} !== undefined) {{")
                    body_lines.append(f"      headers = headers.set('{hname}', {harg});")
                    body_lines.append("    }")

        options_entries: list[str] = []
        if non_conditional_headers:
            options_entries.append("headers")
        if query_params:
            options_entries.append("params: httpParams")
        options_entries.append("withCredentials: true")
        options_literal = "{ " + ", ".join(options_entries) + " }"

        if method in ("get", "delete"):
            body_lines.append(
                f"    return this.http.{method}<{response_type}>({url_expr}, {options_literal});"
            )
        else:
            if is_multipart:
                payload_expr = "formData"
            elif body_type is not None:
                payload_expr = "body" if body_required else "body ?? {}"
            else:
                payload_expr = "{}"
            body_lines.append(
                f"    return this.http.{method}<{response_type}>("
                f"{url_expr}, {payload_expr}, {options_literal});"
            )

        body_lines.append("  }")
        methods.append("\n".join(body_lines))

    # Assemble file header and imports.
    http_imports: list[str] = ["HttpClient"]
    if uses_http_headers:
        http_imports.append("HttpHeaders")
    if uses_http_params:
        http_imports.append("HttpParams")
    if uses_http_response:
        http_imports.append("HttpResponse")

    lines: list[str] = [_GENERATED_HEADER]
    lines.append(f"import {{ {', '.join(http_imports)} }} from '@angular/common/http';")
    lines.append("import { Injectable, inject } from '@angular/core';")
    lines.append("import type { Observable } from 'rxjs';")
    if imported_models:
        lines.append("import type {")
        for model_name in sorted(imported_models):
            lines.append(f"  {model_name},")
        lines.append("} from './models';")
    lines.append("")

    if uses_http_response:
        exported_types.extend(["ConditionalGetBodyOptions", "ConditionalGetResponseOptions"])
        lines.extend(
            [
                "export interface ConditionalGetBodyOptions {",
                "  ifNoneMatch?: string;",
                "  observe?: 'body';",
                "}",
                "",
                "export interface ConditionalGetResponseOptions {",
                "  ifNoneMatch?: string;",
                "  observe: 'response';",
                "}",
                "",
            ]
        )

    for iface in param_interfaces:
        lines.append(iface)
        lines.append("")

    lines.extend(
        [
            "@Injectable({ providedIn: 'root' })",
            "export class OnTrackApiClient {",
            "  private readonly http = inject(HttpClient);",
            "",
        ]
    )
    lines.append("\n\n".join(methods))
    lines.append("}")
    lines.append("")

    return "\n".join(lines), sorted(set(exported_types))


def generate_index_ts(exported_service_types: list[str]) -> str:
    """Generate ``frontend/src/app/api-client/index.ts`` compatible with isolatedModules."""
    lines: list[str] = [
        _GENERATED_HEADER,
        "export { OnTrackApiClient } from './api-client.service';",
    ]
    if exported_service_types:
        lines.append("export type {")
        for type_name in sorted(exported_service_types):
            lines.append(f"  {type_name},")
        lines.append("} from './api-client.service';")
    lines.append("export type * from './models';")
    lines.append("")
    return "\n".join(lines)


def generate_all(schema: dict[str, Any] | None = None) -> GeneratedArtifacts:
    """Generate all deterministic OpenAPI and Angular client files in memory."""
    resolved_schema = export_openapi_schema() if schema is None else schema
    openapi_json = render_openapi_json(resolved_schema)
    # Re-parse serialized JSON so TypeScript generation strictly mirrors backend/openapi.json.
    parsed_schema: dict[str, Any] = json.loads(openapi_json)
    models_ts = generate_models_ts(parsed_schema)
    service_ts, exported_service_types = generate_service_ts(parsed_schema)
    index_ts = generate_index_ts(exported_service_types)
    return GeneratedArtifacts(
        openapi_json=openapi_json,
        models_ts=models_ts,
        service_ts=service_ts,
        index_ts=index_ts,
    )


def target_paths(repo_root: Path | None = None) -> dict[str, Path]:
    """Return output file paths relative to ``repo_root``."""
    root = default_repo_root() if repo_root is None else repo_root
    api_client_dir = root / "frontend" / "src" / "app" / "api-client"
    return {
        "openapi_json": root / "backend" / "openapi.json",
        "models_ts": api_client_dir / "models.ts",
        "service_ts": api_client_dir / "api-client.service.ts",
        "index_ts": api_client_dir / "index.ts",
    }


def write_artifacts(repo_root: Path | None = None) -> list[Path]:
    """Write ``backend/openapi.json`` and ``frontend/src/app/api-client/*`` deterministically."""
    artifacts = generate_all()
    paths = target_paths(repo_root)
    mapping = {
        paths["openapi_json"]: artifacts.openapi_json,
        paths["models_ts"]: artifacts.models_ts,
        paths["service_ts"]: artifacts.service_ts,
        paths["index_ts"]: artifacts.index_ts,
    }
    written: list[Path] = []
    for path, content in mapping.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def check_artifacts(repo_root: Path | None = None) -> list[Path]:
    """Return list of generated artifact paths that are missing or differ from disk."""
    artifacts = generate_all()
    paths = target_paths(repo_root)
    expected = {
        paths["openapi_json"]: artifacts.openapi_json,
        paths["models_ts"]: artifacts.models_ts,
        paths["service_ts"]: artifacts.service_ts,
        paths["index_ts"]: artifacts.index_ts,
    }
    drifted: list[Path] = []
    for path, content in expected.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            drifted.append(path)
    return drifted


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``python -m app.contracts.codegen``."""
    parser = argparse.ArgumentParser(
        prog="python -m app.contracts.codegen",
        description="Export frozen OpenAPI JSON and generate Angular 19 API client.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed artifacts match generated output without writing.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Override repository root directory (defaults to workspace root).",
    )
    args = parser.parse_args(argv)

    if args.check:
        drifted = check_artifacts(args.repo_root)
        if drifted:
            for path in drifted:
                sys.stderr.write(f"DRIFT: {path}\n")
            return 1
        return 0

    written = write_artifacts(args.repo_root)
    for path in written:
        sys.stdout.write(f"Wrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
