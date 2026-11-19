"""Link React/TypeScript callers to backend endpoints via the graph's CALLS_EXTERNAL edges."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

from .endpoints import Endpoint, norm_path
from .flow import UiEntry
from .graph import CodeGraph, ExtCall
from .topology import Topology

_UI_LABELS = {"Component", "Hook"}
_JS = {"javascript", "typescript", "tsx", "jsx", "js", "ts"}


@dataclass
class UiLink:
    component: str
    file: str | None
    function: str
    request_type: str | None
    response_type: str | None
    client: str
    url: str
    line: int

    def as_json(self) -> dict:
        return {k: v for k, v in {
            "component": self.component, "function": self.function, "file": self.file, "client": self.client,
            "url": self.url, "line": self.line, "requestType": self.request_type,
            "responseType": self.response_type}.items() if v not in (None, "")}


def _url_path(url: str) -> str:
    u = re.sub(r"\$\{[^}]*\}", "{}", url)
    if "://" in u:
        u = urlparse(u).path
    elif not u.startswith("/"):
        u = "/" + u.split("/", 1)[1] if "/" in u and not u.split("/", 1)[0].startswith("{") else "/" + u
    return norm_path(u)


class UiLinker:
    def __init__(self, graph: CodeGraph, topo: Topology, endpoints: list[Endpoint], max_hops: int = 6):
        self.graph = graph
        self.max_hops = max_hops
        self.links: dict[str, list[UiLink]] = {}
        self._paths: dict[str, list[Endpoint]] = {}
        for ep in endpoints:
            for p in {norm_path(ep.full_path), *(norm_path(e) for _, e in topo.gateway_paths_for(ep.service, ep.full_path))}:
                self._paths.setdefault(p, []).append(ep)
        self._link(endpoints)

    def _calls(self) -> list[ExtCall]:
        return [x for xs in self.graph.ext_from.values() for x in xs
                if (x.language or self.graph.nodes[x.caller].language or "").lower() in _JS]

    def _match(self, call: ExtCall) -> list[Endpoint]:
        path = _url_path(call.url)
        hits = [e for e in self._paths.get(path, []) if call.method in ("UNKNOWN", e.http_method)]
        if hits:
            return hits
        # base-URL prefixes (e.g. "/v1" added by an axios instance): accept a suffix match of >= 2 segments
        out = []
        for p, eps in self._paths.items():
            if p.count("/") >= 2 and path.endswith(p):
                out += [e for e in eps if call.method in ("UNKNOWN", e.http_method)]
        return out

    def _component_for(self, caller: str) -> tuple[str, str | None]:
        seen = {caller}
        queue = deque([(caller, 0)])
        while queue:
            nid, hops = queue.popleft()
            node = self.graph.nodes.get(nid)
            if node and node.label in _UI_LABELS:
                return node.name, node.path
            # handlers nested in a component (onSubmit inside PayForm) have no CALLS edge from it; use the same file
            same_file = self.graph.ui_nodes.get(node.path or "", []) if node else []
            comp = next((c for c in same_file if c.label == "Component"), None)
            if comp is not None:
                return comp.name, comp.path
            if hops >= self.max_hops:
                continue
            for e in self.graph.callers_of.get(nid, []):
                if e.caller not in seen:
                    seen.add(e.caller)
                    queue.append((e.caller, hops + 1))
        node = self.graph.nodes.get(caller)
        return (node.name if node else caller), (node.path if node else None)

    def _link(self, endpoints: list[Endpoint]) -> None:
        for call in self._calls():
            for ep in self._match(call):
                comp, path = self._component_for(call.caller)
                link = UiLink(comp, path, call.caller_name, call.request_type, call.response_type, call.client,
                              call.url, call.line)
                self.links.setdefault(ep.key + "|" + ep.service, []).append(link)

    def for_endpoint(self, ep: Endpoint) -> list[UiLink]:
        return self.links.get(ep.key + "|" + ep.service, [])

    def entry(self, ep: Endpoint) -> UiEntry | None:
        links = self.for_endpoint(ep)
        if not links:
            return None
        first = links[0]
        extra = f" (+{len(links) - 1} more callers)" if len(links) > 1 else ""
        return UiEntry(first.component, f"{first.function}() via {first.client or 'http client'}{extra}")
