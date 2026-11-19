"""Service topology: application names, properties and Spring Cloud Gateway routes (read from config files)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "dist", ".gradle", ".idea", "test"}
_SECRET_HINT = re.compile(r"(password|secret|token|key|credential|jwt)", re.I)


def parse_properties(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    elif obj is not None:
        out[prefix] = str(obj)
    return out


def unflatten(flat: dict[str, str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for key, val in flat.items():
        parts = re.findall(r"[^.\[\]]+|\[\d+\]", key)
        node: Any = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            is_idx = part.startswith("[")
            nxt_idx = (not last) and parts[i + 1].startswith("[")
            if is_idx:
                idx = int(part[1:-1])
                while len(node) <= idx:
                    node.append(None)
                if last:
                    node[idx] = val
                else:
                    if node[idx] is None:
                        node[idx] = [] if nxt_idx else {}
                    node = node[idx]
            else:
                if last:
                    node[part] = val
                else:
                    node = node.setdefault(part, [] if nxt_idx else {})
    return root


def _ant_to_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("/**", i):
            out += "(?:/.*)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "{":
            j = pattern.index("}", i)
            out += "[^/]+"
            i = j + 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


@dataclass
class GatewayRoute:
    id: str
    uri: str
    service: str | None
    patterns: list[str]
    filters: list[str]
    source: str

    def matches(self, path: str) -> bool:
        return any(_ant_to_regex(p).match(path) for p in self.patterns)

    def downstream_path(self, path: str) -> str:
        """Apply StripPrefix / PrefixPath / RewritePath / SetPath filters to an external path."""
        out = path
        for f in self.filters:
            name, _, arg = f.partition("=")
            name = name.strip()
            if name == "StripPrefix":
                n = int(arg or 1)
                out = "/" + "/".join(out.strip("/").split("/")[n:])
            elif name == "PrefixPath":
                out = arg.rstrip("/") + out
            elif name == "SetPath":
                out = arg
            elif name == "RewritePath":
                rx, _, rep = arg.partition(",")
                rep = re.sub(r"\$\\(\d)", r"\\\1", rep.strip())
                try:
                    out = re.sub(rx.strip(), rep, out)
                except re.error:
                    pass
        return out or "/"

    def external_paths_for(self, downstream: str) -> list[str]:
        """Best-effort reverse mapping: which external paths reach `downstream` through this route."""
        steps = [(f.partition("=")[0].strip(), f.partition("=")[2]) for f in self.filters]
        if any(n in ("RewritePath", "SetPath") for n, _ in steps):
            return []
        cands = [downstream]
        for name, arg in steps:
            if name == "PrefixPath":
                pre = arg.rstrip("/")
                cands = [c[len(pre):] or "/" for c in cands if c.startswith(pre)]
            elif name == "StripPrefix":
                n = int(arg or 1)
                new = []
                for p in self.patterns:
                    lit = [s for s in p.strip("/").split("/") if not any(ch in s for ch in "*{")]
                    if len(lit) >= n:
                        new += ["/" + "/".join(lit[:n]) + (c if c != "/" else "") for c in cands]
                cands = new
        return [c for c in cands if self.matches(c)]


@dataclass
class ServiceInfo:
    name: str
    module: str
    props: dict[str, str] = field(default_factory=dict)

    @property
    def context_path(self) -> str:
        cp = self.props.get("server.servlet.context-path", "") or ""
        return cp.rstrip("/") if cp not in ("", "/") else ""

    def safe_props(self) -> dict[str, str]:
        return {k: v for k, v in self.props.items() if not _SECRET_HINT.search(k)}


class Topology:
    """Maps modules to service names and exposes gateway routing derived from configuration files."""

    def __init__(self, root: Path, modules: set[str]):
        self.root = root.resolve()
        self.services: dict[str, ServiceInfo] = {}
        self.by_module: dict[str, ServiceInfo] = {}
        self.routes: list[GatewayRoute] = []
        self._load(modules)

    # -- loading
    def _config_files(self) -> list[Path]:
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if re.match(r"^(application|bootstrap)?[\w.-]*\.(properties|ya?ml)$", fn) and \
                        fn not in ("docker-compose.yml", "docker-compose.yaml") and "docker-compose" not in fn:
                    found.append(Path(dirpath) / fn)
        return found

    @staticmethod
    def _read(path: Path) -> dict[str, str]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        if path.suffix == ".properties":
            return parse_properties(text)
        flat: dict[str, str] = {}
        try:
            for doc in yaml.safe_load_all(text):
                flat.update(flatten(doc))
        except yaml.YAMLError:
            return {}
        return flat

    def _load(self, modules: set[str]) -> None:
        files = self._config_files()
        parsed = {f: self._read(f) for f in files}

        for module in sorted(modules):
            name = None
            props: dict[str, str] = {}
            res = Path(module) / "src" / "main" / "resources"
            for f, p in parsed.items():
                if res in f.parents or f.parent == res:
                    props.update(p)
                    name = name or p.get("spring.application.name")
            name = name or Path(module).name
            svc = ServiceInfo(name=name, module=module, props=props)
            self.services[name] = svc
            self.by_module[module] = svc

        # central config files named after a service (Spring Cloud Config style: <service>.properties)
        # base files (<service>.properties) take precedence over profile variants (<service>-docker.properties)
        for f, p in sorted(parsed.items(), key=lambda kv: re.search(r"-(docker|dev|prod|local|test|default)$", kv[0].stem) is not None):
            stem = re.sub(r"-(docker|dev|prod|local|test|default)$", "", f.stem)
            if stem in self.services and not (Path(self.services[stem].module) in f.parents):
                self.services[stem].props.update({k: v for k, v in p.items() if k not in self.services[stem].props})

        for f, p in parsed.items():
            self._collect_routes(f, p)

    def _collect_routes(self, file: Path, flat: dict[str, str]) -> None:
        sub = {k: v for k, v in flat.items() if "gateway" in k and ".routes[" in k}
        if not sub:
            return
        tree = unflatten(sub)
        node: Any = tree
        for key in ("spring", "cloud", "gateway"):
            node = node.get(key, {}) if isinstance(node, dict) else {}
        routes = None
        for cand in (node.get("routes"), (node.get("server") or {}).get("webflux", {}).get("routes"),
                     (node.get("mvc") or {}).get("routes")):
            if cand:
                routes = cand
                break
        for r in routes or []:
            if not isinstance(r, dict):
                continue
            uri = str(r.get("uri", ""))
            patterns: list[str] = []
            for p in r.get("predicates") or []:
                text = p if isinstance(p, str) else f"{p.get('name', '')}={_args(p.get('args'))}"
                if text.startswith("Path="):
                    patterns += [s.strip() for s in text[5:].split(",") if s.strip()]
            filters = [f if isinstance(f, str) else f"{f.get('name', '')}={_args(f.get('args'))}"
                       for f in (r.get("filters") or [])]
            if uri.startswith("lb://"):
                service = uri[5:].split("/")[0]
            else:
                service = urlparse(uri).hostname
            self.routes.append(GatewayRoute(str(r.get("id", "")), uri, service, patterns, filters, str(file)))

    # -- queries
    def service_for_module(self, module: str) -> ServiceInfo:
        return self.by_module.get(module) or ServiceInfo(Path(module).name, module)

    @staticmethod
    def _port(svc: ServiceInfo) -> str | None:
        raw = svc.props.get("server.port", "")
        m = re.fullmatch(r"\$\{[^:}]+:?([^}]*)\}", raw.strip())
        port = (m.group(1) if m else raw).strip()
        return port if port.isdigit() and int(port) > 0 else None

    def gateway_service(self) -> ServiceInfo | None:
        return next((s for s in self.services.values()
                     if any("gateway" in k and ".routes[" in k for k in s.props)), None)

    def base_url(self, svc: ServiceInfo | None) -> str | None:
        """http://localhost:<port> for a service, or lb://<name> when the port is dynamic (server.port=0)."""
        if svc is None:
            return None
        port = self._port(svc)
        return f"http://localhost:{port}" if port else f"lb://{svc.name}"

    def route_for_external_path(self, path: str) -> GatewayRoute | None:
        return next((r for r in self.routes if r.matches(path)), None)

    def gateway_paths_for(self, service: str, downstream_path: str) -> list[tuple[GatewayRoute, str]]:
        out: list[tuple[GatewayRoute, str]] = []
        for r in self.routes:
            if r.service != service:
                continue
            for ext in r.external_paths_for(downstream_path):
                out.append((r, ext))
        return out


def _args(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, dict):
        return ",".join(str(v) for v in args.values())
    return str(args)
