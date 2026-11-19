"""Write per-API JSON (OpenAPI-style operation) and Mermaid sequence diagrams to disk."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .endpoints import Endpoint
from .flow import Diagram, Tracer
from .mermaid import render
from .schema import example_for
from .topology import Topology
from .ui import UiLinker


def slug(ep: Endpoint) -> str:
    path = re.sub(r"\{([^}]*)\}", r"by-\1", ep.full_path.strip("/")) or "root"
    path = re.sub(r"[^A-Za-z0-9._-]+", "_", path)
    return f"{ep.http_method}_{path}"


def _content(ct: list[str] | str | None, schema: dict[str, Any], example: Any = None) -> dict[str, Any]:
    cts = [ct] if isinstance(ct, str) else (ct or ["application/json"])
    body: dict[str, Any] = {"schema": schema}
    body["example"] = example if example is not None else example_for(schema)
    return {c: body for c in cts}


def entry_point(ep: Endpoint, topo: Topology) -> dict[str, Any]:
    """What a client calls first: the gateway URL when a route exists, otherwise the service URL."""
    svc_base = topo.base_url(topo.services.get(ep.service))
    direct = f"{svc_base}{ep.full_path}" if svc_base else ep.full_path
    routes = topo.gateway_paths_for(ep.service, ep.full_path)
    gw_base = topo.base_url(topo.gateway_service())
    if routes and gw_base:
        route, ext = routes[0]
        return {"calledFirst": f"{ep.http_method} {ext}", "fullUrl": f"{gw_base}{ext}", "via": "api-gateway",
                "gatewayRoute": route.id, "serviceUrl": direct}
    return {"calledFirst": f"{ep.http_method} {ep.full_path}", "fullUrl": direct, "via": "direct",
            "serviceUrl": direct}


def operation(ep: Endpoint, topo: Topology, ui: UiLinker | None, diagram: Diagram, diagram_path: str) -> dict[str, Any]:
    params = [{"name": p.name, "in": p.location, "required": p.required, "schema": p.schema,
               **({"default": p.default} if p.default is not None else {}),
               **({"description": p.description} if p.description else {})}
              for p in ep.params]
    op: dict[str, Any] = {
        "service": ep.service,
        "operationId": ep.operation,
        "method": ep.http_method,
        "path": ep.full_path,
        "summary": (ep.method.doc or "").strip(),
        "controller": ep.cls.fqn,
        "parameters": params,
    }
    routes = topo.gateway_paths_for(ep.service, ep.full_path)
    if routes:
        op["gateway"] = [{"routeId": r.id, "externalPath": ext} for r, ext in routes]
    op["entryPoint"] = entry_point(ep, topo)
    if ep.body:
        ct = ep.consumes or ["application/json"]
        op["requestBody"] = {
            "required": ep.body.required,
            "description": ep.body.description,
            "content": _content(ct, ep.body.schema),
        }
    responses = {}
    for code, r in sorted(ep.responses.items()):
        entry: dict[str, Any] = {"description": r.get("description", "")}
        if "schema" in r:
            entry["content"] = _content(r.get("contentType") or ep.produces, r["schema"], r.get("example"))
        responses[code] = entry
    op["responses"] = responses
    if ep.error_responses:
        op["errorResponses"] = ep.error_responses
    links = ui.for_endpoint(ep) if ui else []
    if links:
        op["calledBy"] = [lk.as_json() for lk in links]
    op["sequenceDiagram"] = diagram_path
    if diagram.warnings:
        op["traceWarnings"] = sorted(set(diagram.warnings))
    return op


def write_all(out_dir: Path, repo_name: str, endpoints: list[Endpoint], tracer: Tracer, topo: Topology,
              ui: UiLinker | None) -> dict[str, Any]:
    root = out_dir / repo_name
    seen: Counter[str] = Counter()
    rows: list[tuple[Endpoint, str, int, int]] = []
    for ep in endpoints:
        base = f"{ep.service}/{slug(ep)}"
        seen[base] += 1
        if seen[base] > 1:
            base += f"_{seen[base]}"
        entry = ui.entry(ep) if ui else None
        diagram = tracer.trace(ep, entry)
        mmd = root / "sequence" / f"{base}.mmd"
        js = root / "apis" / f"{base}.json"
        mmd.parent.mkdir(parents=True, exist_ok=True)
        js.parent.mkdir(parents=True, exist_ok=True)
        mmd.write_text(render(diagram), encoding="utf-8")
        rel = f"../../sequence/{base}.mmd"
        js.write_text(json.dumps(operation(ep, topo, ui, diagram, rel), indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
        rows.append((ep, base, len(diagram.participants), len(diagram.warnings)))

    lines = [f"# {repo_name} — API catalogue", "", "| Service | Entry point (full URL) | Request/Response | Sequence | Participants | Warnings |",
             "|---|---|---|---|---|---|"]
    for ep, base, n, w in rows:
        lines.append(f"| {ep.service} | `{ep.http_method} {entry_point(ep, topo)['fullUrl']}` | [json](apis/{base}.json) | "
                     f"[mmd](sequence/{base}.mmd) | {n} | {w} |")
    (root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"output": str(root), "apis": len(rows), "warnings": sum(r[3] for r in rows)}
