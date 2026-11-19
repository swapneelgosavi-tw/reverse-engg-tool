"""Sequence-flow tracing over the call graph plus source-level facts (HTTP, messaging, data stores)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from tree_sitter import Node

from .endpoints import Endpoint, norm_path
from .graph import CallEdge, CodeGraph, ExtCall, MethodId, parse_method_id
from .javamodel import ClassInfo, JavaProject, MethodInfo, TypeRef
from .schema import SchemaBuilder, unwrap_type
from .topology import Topology

# ---------------------------------------------------------------------------- diagram model


@dataclass
class Participant:
    alias: str
    label: str
    kind: str  # actor | gateway | controller | service | repository | client | database | cache | broker | external | ui
    group: str | None = None


@dataclass
class Call:
    src: str
    dst: str
    text: str
    ret: str | None = None  # None -> no return arrow (fire and forget or leaf infra)
    is_async: bool = False
    children: list[Any] = field(default_factory=list)


@dataclass
class Note:
    over: str
    text: str


@dataclass
class Block:
    kind: str  # alt | opt | loop
    branches: list[tuple[str, list[Any]]]


@dataclass
class Diagram:
    title: str
    participants: dict[str, Participant] = field(default_factory=dict)
    items: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- source structure helpers


@dataclass
class _Branch:
    label: str
    start: int
    end: int
    blocks: list["_SBlock"]


@dataclass
class _SBlock:
    kind: str
    start: int
    end: int
    branches: list[_Branch]


def _txt(n: Node | None) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _clean(s: str, limit: int = 48) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace(";", ",")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _rng(n: Node) -> tuple[int, int]:
    return n.start_point.row + 1, n.end_point.row + 1


def _blocks_in(node: Node) -> list[_SBlock]:
    out: list[_SBlock] = []
    for ch in node.children:
        out += _blocks_of(ch)
    return out


def _blocks_of(n: Node) -> list[_SBlock]:
    t = n.type
    if t == "if_statement":
        cond = _clean(_txt(n.child_by_field_name("condition")).strip("()"))
        cons = n.child_by_field_name("consequence")
        alt = n.child_by_field_name("alternative")
        branches = []
        if cons is not None:
            s, e = _rng(cons)
            branches.append(_Branch(cond, s, e, _blocks_in(cons)))
        if alt is not None:
            s, e = _rng(alt)
            branches.append(_Branch("else", s, e, _blocks_in(alt) if alt.type != "if_statement" else _blocks_of(alt)))
        s, e = _rng(n)
        return [_SBlock("if", s, e, branches)]
    if t in ("for_statement", "enhanced_for_statement", "while_statement", "do_statement"):
        body = n.child_by_field_name("body")
        if t == "enhanced_for_statement":
            label = "for each " + _clean(_txt(n.child_by_field_name("name")) + " in " + _txt(n.child_by_field_name("value")))
        elif t == "for_statement":
            label = "for loop"
        else:
            label = "while " + _clean(_txt(n.child_by_field_name("condition")).strip("()"))
        s, e = _rng(n)
        inner = _blocks_in(body) if body is not None else []
        return [_SBlock("loop", s, e, [_Branch(label, s, e, inner)])]
    if t == "try_statement":
        res: list[_SBlock] = []
        for c in n.children:
            if c.type == "block":
                res += _blocks_in(c)
            elif c.type == "catch_clause":
                typ = _clean(_txt(next((x for x in c.children if x.type == "catch_formal_parameter"), None)).split(" ")[0])
                body = c.child_by_field_name("body")
                s, e = _rng(c)
                res.append(_SBlock("catch", s, e, [_Branch(f"catch {typ}", s, e, _blocks_in(body) if body else [])]))
            elif c.type == "finally_clause":
                res += _blocks_in(c)
            elif c.type == "resource_specification":
                continue
        return res
    if t in ("lambda_expression", "class_body", "method_declaration"):
        return []
    return _blocks_in(n)


# ---------------------------------------------------------------------------- tracer

_STEREOTYPES_SERVICE = {"Service", "Component", "Named", "Singleton", "Stateless", "Stateful", "ApplicationScoped",
                        "RequestScoped"}
_NAME_SUFFIX = ("Service", "ServiceImpl", "Manager", "Facade", "Handler", "Gateway", "Adapter", "Processor",
                "Producer", "Consumer", "Listener", "Publisher", "Dao", "DAO", "Client", "Repository", "Controller",
                "Orchestrator", "Validator", "Provider")
_NOISE_SUFFIX = ("Dto", "DTO", "Request", "Response", "Entity", "Event", "Exception", "Config", "Configuration",
                 "Properties", "Mapper", "Converter", "Util", "Utils", "Constants", "Model", "Builder", "Test")
_REPO_SUPER = re.compile(r"(Repository|JpaRepository|CrudRepository|MongoRepository|Dao)$")

_BROKER_FIELD = {"KafkaTemplate": "Kafka", "ReactiveKafkaProducerTemplate": "Kafka", "RabbitTemplate": "RabbitMQ",
                 "AmqpTemplate": "RabbitMQ", "JmsTemplate": "JMS", "StreamBridge": "Stream", "JmsMessagingTemplate": "JMS"}
_BROKER_SEND = {"send", "sendDefault", "convertAndSend", "send", "sendAndReceive", "convertSendAndReceive",
                "publish", "sendMessage"}
_CACHE_FIELD = {"RedisTemplate": "Redis", "StringRedisTemplate": "Redis", "ReactiveRedisTemplate": "Redis",
                "RedisOperations": "Redis", "RedissonClient": "Redis", "CacheManager": "Cache", "Cache": "Cache",
                "HazelcastInstance": "Hazelcast"}
_DB_FIELD = {"JdbcTemplate": "DB", "NamedParameterJdbcTemplate": "DB", "EntityManager": "DB", "JdbcClient": "DB",
             "SimpleJdbcCall": "DB", "SimpleJdbcInsert": "DB", "MongoTemplate": "MongoDB",
             "ReactiveMongoTemplate": "MongoDB", "ElasticsearchOperations": "Elasticsearch",
             "R2dbcEntityTemplate": "DB", "DatabaseClient": "DB", "SessionFactory": "DB", "Session": "DB"}
_OTHER_FIELD = {"JavaMailSender": "Mail", "MailSender": "Mail"}
_RESILIENCE = {"CircuitBreaker", "Retry", "TimeLimiter", "RateLimiter", "Bulkhead"}
_SECURITY = {"PreAuthorize", "PostAuthorize", "Secured", "RolesAllowed"}
_CACHE_ANN = {"Cacheable", "CacheEvict", "CachePut"}
_LISTENERS = {"KafkaListener": ("topics", "topicPattern", "value"), "RabbitListener": ("queues", "value"),
              "JmsListener": ("destination", "value"), "SqsListener": ("value", "queueNames"),
              "StreamListener": ("value",), "RabbitHandler": ()}


@dataclass
class _Ctx:
    diagram: Diagram
    stack: list[str] = field(default_factory=list)
    depth: int = 0


class Tracer:
    def __init__(self, project: JavaProject, graph: CodeGraph, topo: Topology, endpoints: list[Endpoint],
                 sb: SchemaBuilder, min_confidence: float = 0.9, max_depth: int = 14):
        self.project = project
        self.graph = graph
        self.topo = topo
        self.endpoints = endpoints
        self.sb = sb
        self.min_conf = min_confidence
        self.max_depth = max_depth
        self.ep_by_service: dict[str, list[Endpoint]] = {}
        for ep in endpoints:
            self.ep_by_service.setdefault(ep.service, []).append(ep)
        self.ep_by_method: dict[int, Endpoint] = {id(ep.method): ep for ep in endpoints}
        self._gid_index: dict[tuple[str, str], list[str]] = {}
        for gid in list(graph.calls_from) + list(graph.ext_from):
            mid = parse_method_id(gid)
            if mid:
                self._gid_index.setdefault((mid.cls_fqn, mid.name), []).append(gid)
        self._struct_cache: dict[int, list[_SBlock]] = {}
        self._consumers: dict[str, list[tuple[ClassInfo, MethodInfo]]] = {}
        self._consts = self._collect_consts()
        self._index_consumers()
        self._all_props: dict[str, str] = {}
        for svc in topo.services.values():
            for k, v in svc.props.items():
                self._all_props.setdefault(k, v)

    # ------------------------------------------------------------------ public
    def trace(self, ep: Endpoint, ui: "UiEntry | None" = None) -> Diagram:
        d = Diagram(title=f"{ep.http_method} {ep.full_path}")
        ctx = _Ctx(d)
        start_label = ui.label if ui else "Client"
        client = self._participant(d, "ui" if ui else "actor", start_label, None, alias_hint="Client")
        gateway = None
        routes = self.topo.gateway_paths_for(ep.service, ep.full_path)
        route = routes[0] if routes else None
        if route is not None:
            gateway = self._participant(d, "gateway", "API Gateway", None, alias_hint="Gateway")
        ctrl = self._class_participant(d, ep.cls)
        req = ep.body.type if ep.body else ""
        ext_path = route[1] if route else ep.full_path
        out_label = f"{ep.http_method} {ext_path}" + (f" ({req})" if req else "")
        ret = self._http_return(ep)
        items: list[Any] = []
        if ui is not None:
            items.append(Note(client.alias, ui.note))
        gw_base = self.topo.base_url(self.topo.gateway_service()) if route else None
        svc_base = self.topo.base_url(self.topo.services.get(ep.service))
        full_url = f"{gw_base}{ext_path}" if gw_base else (f"{svc_base}{ep.full_path}" if svc_base else None)
        if full_url:
            items.append(Note(client.alias, f"{ep.http_method} {full_url}"))
        children = self._entry_children(ep, ctx, ctrl)
        if gateway is not None:
            inner = Call(gateway.alias, ctrl.alias, f"{ep.http_method} {ep.full_path}" + (f" ({req})" if req else ""),
                         ret, children=children)
            items.append(Call(client.alias, gateway.alias, out_label, ret, children=[inner]))
        else:
            items.append(Call(client.alias, ctrl.alias, out_label, ret, children=children))
        d.items = items
        return d

    # ------------------------------------------------------------------ participants
    def _participant(self, d: Diagram, kind: str, label: str, group: str | None, alias_hint: str | None = None) -> Participant:
        base = re.sub(r"[^A-Za-z0-9_]", "_", alias_hint or label) or "P"
        for p in d.participants.values():
            if p.alias.startswith(base) and (p.kind, p.group, p.label) == (kind, group, label):
                return p
        alias, i = base, 2
        while alias in d.participants:
            alias = f"{base}_{i}"
            i += 1
        p = Participant(alias, label, kind, group)
        d.participants[alias] = p
        return p

    def _class_participant(self, d: Diagram, cls: ClassInfo) -> Participant:
        svc = self.topo.service_for_module(cls.module).name
        role = self.role(cls) or "service"
        return self._participant(d, role, cls.name, svc)

    # ------------------------------------------------------------------ classification
    def role(self, cls: ClassInfo) -> str | None:
        if cls.kind in ("enum", "record"):
            return None
        names = {a.name for a in cls.annotations}
        if "FeignClient" in names:
            return "client"
        if cls.has_ann("Configuration", "ConfigurationProperties", "SpringBootApplication") and not any(
                m.ann(*_LISTENERS) for m in cls.methods):
            return None
        if cls.kind == "interface" and self.is_data_repo(cls):
            return "repository"
        if names & {"RestController", "Controller"}:
            return "controller"
        if "Repository" in names:
            return "repository"
        if names & _STEREOTYPES_SERVICE or any(m.ann(*_LISTENERS) for m in cls.methods):
            return "service"
        if names & {"Entity", "Document", "Table", "Embeddable", "MappedSuperclass", "Data", "Value"}:
            return None
        if cls.name.endswith(_NOISE_SUFFIX):
            return None
        if cls.name.endswith(_NAME_SUFFIX):
            return "repository" if cls.name.endswith(("Dao", "DAO", "Repository")) and cls.kind == "interface" else "service"
        # interfaces implemented by a participant (e.g. AccountService -> AccountServiceImpl)
        if cls.kind == "interface" and self.impls_of(cls):
            return "service"
        return None

    def is_data_repo(self, cls: ClassInfo) -> bool:
        return any(_REPO_SUPER.search(t.simple) for t in cls.interfaces)

    def impls_of(self, iface: ClassInfo) -> list[ClassInfo]:
        out = []
        for c in self.project.classes.values():
            if c.kind != "class" or c.fqn == iface.fqn:
                continue
            for t in ([c.superclass] if c.superclass else []) + c.interfaces:
                r = self.project.resolve(t.name, c)
                if r is not None and r.fqn == iface.fqn:
                    out.append(c)
                    break
        out.sort(key=lambda c: (not c.has_ann("Primary"), c.fqn))
        return out

    # ------------------------------------------------------------------ method lookup
    def _gid(self, m: MethodInfo) -> str | None:
        cls = m.cls
        assert cls is not None
        exact = f"method:{cls.fqn}.{m.name}({m.signature_key.replace(' ', '')})"
        if exact in self.graph.calls_from or exact in self.graph.ext_from:
            return exact
        cands = self._gid_index.get((cls.fqn, m.name), [])
        by_arity = [g for g in cands if (parse_method_id(g) or MethodId("", "", "")).arity == len(m.params)]
        if len(by_arity) == 1:
            return by_arity[0]
        return by_arity[0] if by_arity else (cands[0] if len(cands) == 1 else None)

    def _find_method(self, cls: ClassInfo, mid: MethodId) -> MethodInfo | None:
        pool = [m for m in cls.methods if m.name == mid.name]
        if not pool:
            for sup in self._supers(cls):
                pool = [m for m in sup.methods if m.name == mid.name]
                if pool:
                    break
        if not pool:
            return None
        exact = [m for m in pool if m.signature_key.replace(" ", "") == mid.params]
        if exact:
            return exact[0]
        by_arity = [m for m in pool if len(m.params) == mid.arity]
        return by_arity[0] if by_arity else pool[0]

    def _supers(self, cls: ClassInfo, seen: set[str] | None = None) -> list[ClassInfo]:
        seen = seen or {cls.fqn}
        out: list[ClassInfo] = []
        for s in self.project.supertypes(cls):
            if s.fqn not in seen:
                seen.add(s.fqn)
                out.append(s)
                out += self._supers(s, seen)
        return out

    # ------------------------------------------------------------------ expansion
    def _entry_children(self, ep: Endpoint, ctx: _Ctx, ctrl: Participant) -> list[Any]:
        items = self._notes_for(ep.method, ctrl.alias)
        items += self.expand(ep.method, ctx, ctrl)
        return items

    def expand(self, method: MethodInfo, ctx: _Ctx, here: Participant) -> list[Any]:
        cls = method.cls
        assert cls is not None
        key = f"{cls.fqn}.{method.name}/{len(method.params)}"
        if key in ctx.stack:
            return []
        if ctx.depth >= self.max_depth:
            ctx.diagram.warnings.append(f"depth limit reached at {cls.name}.{method.name}")
            return []
        ctx.stack.append(key)
        ctx.depth += 1
        try:
            events: list[tuple[int, Any]] = []
            gid = self._gid(method)
            if gid:
                edges: list[tuple[int, int, Any]] = [(e.line, e.seq, e) for e in self.graph.calls_from.get(gid, [])]
                edges += [(x.line, x.seq, x) for x in self.graph.ext_from.get(gid, [])]
                edges.sort(key=lambda t: (t[0], t[1]))
                for line, _seq, e in edges:
                    if isinstance(e, ExtCall):
                        events.append((line, self._http_item(here, e, ctx)))
                    elif e.confidence >= self.min_conf:
                        item = self._call_item(cls, here, e, ctx)
                        if item is not None:
                            events.append((line, item))
            events += self._infra_items(method, here, ctx)
            events.sort(key=lambda t: t[0])
            return self._layout(events, self._structure(method))
        finally:
            ctx.stack.pop()
            ctx.depth -= 1

    def _call_item(self, caller_cls: ClassInfo, here: Participant, e: CallEdge, ctx: _Ctx) -> Any | None:
        mid = parse_method_id(e.target)
        if mid is None:
            return None
        tcls = self.project.classes.get(mid.cls_fqn)
        if tcls is None:
            return None
        role = self.role(tcls)
        if role is None:
            return None
        tmeth = self._find_method(tcls, mid)
        label = self._label(tmeth, mid)

        if tcls.fqn == caller_cls.fqn:
            sub = self.expand(tmeth, ctx, here) if tmeth is not None else []
            if not sub:
                return None
            return Call(here.alias, here.alias, label, self._ret_label(tmeth), children=sub)

        d = ctx.diagram
        alts: list[ClassInfo] = []
        if tcls.kind == "interface" and role not in ("client", "repository"):
            impls = self.impls_of(tcls)
            if impls:
                alts = impls[1:]
                tcls = impls[0]
                tmeth = self._find_method(tcls, mid) or tmeth
        dst = self._class_participant(d, tcls)
        children: list[Any] = []
        if alts:
            children.append(Note(dst.alias, "other implementations: " + ", ".join(a.name for a in alts)))
        if role == "repository" and tcls.kind == "interface":
            children += self._repo_items(tcls, tmeth, mid, dst, ctx)
        elif tmeth is not None:
            children += self._notes_for(tmeth, dst.alias)
            children += self.expand(tmeth, ctx, dst)
        return Call(here.alias, dst.alias, label, self._ret_label(tmeth), children=children)

    # ------------------------------------------------------------------ repositories / data stores
    def _repo_items(self, repo: ClassInfo, tmeth: MethodInfo | None, mid: MethodId, dst: Participant, ctx: _Ctx) -> list[Any]:
        kind, table, entity = self._repo_store(repo)
        store = self._participant(ctx.diagram, "database", f"{kind}: {table}" if table else kind, dst.group,
                                  alias_hint=f"DB_{table or repo.name}")
        text = mid.name
        if tmeth is not None:
            q = tmeth.ann("Query")
            if q is not None and isinstance(q.get("value"), str):
                text = f"{mid.name}: {_clean(q.get('value'), 60)}"
        return [Call(dst.alias, store.alias, text, None)]

    def _repo_store(self, repo: ClassInfo) -> tuple[str, str, ClassInfo | None]:
        kind = "DB"
        entity = None
        for t in repo.interfaces:
            if "Mongo" in t.simple:
                kind = "MongoDB"
            if "Elasticsearch" in t.simple:
                kind = "Elasticsearch"
            if t.args:
                entity = self.project.resolve(t.args[0].name, repo) or entity
        table = ""
        if entity is not None:
            for a in entity.annotations:
                if a.name == "Table":
                    table = str(a.get("name") or "")
                elif a.name in ("Document", "Entity"):
                    kind = "MongoDB" if a.name == "Document" and kind == "DB" else kind
                    table = str(a.get("collection", "value", "name") or table)
                elif a.name == "Container":
                    table = str(a.get("containerName") or "")
            table = table or entity.name
        return kind, table, entity

    def _infra_items(self, method: MethodInfo, here: Participant, ctx: _Ctx) -> list[tuple[int, Any]]:
        if method.body is None or method.cls is None:
            return []
        fields = {f.name: f for c in [method.cls] + self._supers(method.cls) for f in c.fields}
        out: list[tuple[int, Any]] = []
        stack = [method.body]
        while stack:
            n = stack.pop()
            stack.extend(reversed(n.children))
            if n.type != "method_invocation":
                continue
            name = _txt(n.child_by_field_name("name"))
            base = self._base_ident(n.child_by_field_name("object"))
            if base is None or base not in fields:
                continue
            ftype = fields[base].type.simple
            line = n.start_point.row + 1
            args = n.child_by_field_name("arguments")
            argn = [c for c in (args.named_children if args else []) if c.type != "comment"]
            if ftype in _BROKER_FIELD and name in _BROKER_SEND:
                out.append((line, self._broker_item(ftype, name, argn, method, here, ctx)))
            elif ftype in _CACHE_FIELD:
                if name.startswith("opsFor") or name in ("boundValueOps",):
                    continue
                key = self._arg_text(argn[0], method) if argn else ""
                kind = _CACHE_FIELD[ftype]
                store = self._participant(ctx.diagram, "cache", kind, here.group)
                out.append((line, Call(here.alias, store.alias, f"{name}({key})" if key else name, None)))
            elif ftype in _DB_FIELD:
                kind = _DB_FIELD[ftype]
                sql = ""
                if argn and argn[0].type == "string_literal":
                    sql = _clean(_txt(argn[0]).strip('"'), 60)
                store = self._participant(ctx.diagram, "database", kind, here.group)
                out.append((line, Call(here.alias, store.alias, f"{name}: {sql}" if sql else name, None)))
            elif ftype in _OTHER_FIELD and name.startswith("send"):
                store = self._participant(ctx.diagram, "external", _OTHER_FIELD[ftype], here.group)
                out.append((line, Call(here.alias, store.alias, name, None)))
        return out

    @staticmethod
    def _base_ident(n: Node | None) -> str | None:
        while n is not None:
            if n.type == "identifier":
                return _txt(n)
            if n.type == "field_access":
                obj = n.child_by_field_name("object")
                if obj is not None and obj.type == "this":
                    return _txt(n.child_by_field_name("field"))
                n = obj
            elif n.type == "method_invocation":
                n = n.child_by_field_name("object")
            else:
                return None
        return None

    def _arg_text(self, n: Node, method: MethodInfo) -> str:
        if n.type == "string_literal":
            return _clean(_txt(n).strip('"'), 40)
        if n.type in ("identifier", "field_access"):
            key = _txt(n).rsplit(".", 1)[-1]
            return str(self._consts.get(key, key))
        return ""

    # ------------------------------------------------------------------ messaging
    def _broker_item(self, ftype: str, name: str, args: list[Node], method: MethodInfo, here: Participant,
                     ctx: _Ctx) -> Call:
        kind = _BROKER_FIELD[ftype]
        if name == "sendDefault":
            dest = self._all_props.get("spring.kafka.template.default-topic", "")
            payload_node = args[-1] if args else None
        elif ftype in ("RabbitTemplate", "AmqpTemplate"):
            dest = self._arg_text(args[1], method) if len(args) > 2 else (self._arg_text(args[0], method) if args else "")
            payload_node = args[-1] if args else None
        else:
            dest = self._arg_text(args[0], method) if args else ""
            payload_node = args[-1] if len(args) > 1 else None
        payload = self._payload_type(payload_node, method)
        broker = self._participant(ctx.diagram, "broker", f"{kind}: {dest}" if dest else kind, here.group,
                                   alias_hint=f"{kind}_{dest}" if dest else kind)
        children: list[Any] = []
        for cls, m in self._consumers.get(dest, []):
            cp = self._class_participant(ctx.diagram, cls)
            sub = self._notes_for(m, cp.alias) + self.expand(m, ctx, cp)
            children.append(Call(broker.alias, cp.alias, m.label, None, is_async=True, children=sub))
        text = f"{name}" + (f"({payload})" if payload else "")
        return Call(here.alias, broker.alias, text, None, is_async=True, children=children)

    def _payload_type(self, n: Node | None, method: MethodInfo) -> str:
        if n is None:
            return ""
        if n.type == "object_creation_expression":
            return _txt(n.child_by_field_name("type")).split("<")[0]
        if n.type == "identifier":
            nm = _txt(n)
            for p in method.params:
                if p.name == nm:
                    return p.type.simple
        return ""

    def _collect_consts(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for c in self.project.classes.values():
            for f in c.fields:
                if f.const_value is not None and "static" in f.modifiers:
                    out.setdefault(f.name, str(f.const_value))
        return out

    def _index_consumers(self) -> None:
        for c in self.project.classes.values():
            for m in c.methods:
                for a in m.annotations:
                    keys = _LISTENERS.get(a.name)
                    if keys is None:
                        continue
                    vals: list[Any] = []
                    for k in keys:
                        v = a.get(k)
                        if v is not None:
                            vals += v if isinstance(v, list) else [v]
                    for v in vals:
                        topic = self._resolve_placeholder(str(v))
                        self._consumers.setdefault(topic, []).append((c, m))

    def _resolve_placeholder(self, v: str) -> str:
        m = re.fullmatch(r"\$\{([^:}]+)(?::([^}]*))?\}", v)
        if not m:
            return self._consts.get(v.rsplit(".", 1)[-1], v) if re.fullmatch(r"[A-Z_][A-Z0-9_.]*", v) else v
        props = {k: val for svc in self.topo.services.values() for k, val in svc.props.items()}
        return props.get(m.group(1), m.group(2) or v)

    # ------------------------------------------------------------------ HTTP
    def _http_item(self, here: Participant, x: ExtCall, ctx: _Ctx) -> Call:
        d = ctx.diagram
        url = re.sub(r"\$\{[^}]+\}", lambda mo: self._resolve_placeholder(mo.group(0)), x.url)
        parsed = urlparse(url if "://" in url else "http://" + url.lstrip("/"))
        path = parsed.path or "/"
        target = self._resolve_endpoint(x.method, parsed.hostname or "", parsed.port, path)
        if target is not None:
            dst = self._class_participant(d, target.cls)
            sub = self._notes_for(target.method, dst.alias) + self.expand(target.method, ctx, dst)
            qnames = [p.name for p in target.params if p.location == "query"]
            text = f"{target.http_method} {path}" + (f"?{'&'.join(q + '={' + q + '}' for q in qnames)}" if qnames else "")
            if target.body:
                text += f" ({target.body.type})"
            return Call(here.alias, dst.alias, text, self._http_return(target), children=sub)
        host = parsed.hostname or x.host or "external"
        ext = self._participant(d, "external", f"External: {host}", here.group, alias_hint=f"Ext_{host}")
        d.warnings.append(f"unresolved outbound call {x.method} {x.url}")
        ret = x.response_type or "response"
        return Call(here.alias, ext.alias, f"{x.method} {path}" + (f" ({x.request_type})" if x.request_type else ""),
                    ret, children=[])

    def _resolve_endpoint(self, method: str, host: str, port: int | None, path: str) -> Endpoint | None:
        svc_name = None
        if host in self.topo.services:
            svc_name = host
        elif port is not None:
            for s in self.topo.services.values():
                if str(s.props.get("server.port", "")) == str(port):
                    svc_name = s.name
                    break
        if svc_name is None:
            route = self.topo.route_for_external_path(path)
            if route is not None and route.service in self.topo.services:
                svc_name = route.service
                path = route.downstream_path(path)
        if svc_name is None:
            return None
        np = norm_path(path)
        for ep in self.ep_by_service.get(svc_name, []):
            if ep.http_method in (method, "ANY") or method in ("UNKNOWN", "ANY"):
                if norm_path(ep.full_path) == np or norm_path(ep.path) == np:
                    return ep
        return None

    def _http_return(self, ep: Endpoint) -> str:
        if not ep.responses:
            return "response"
        code = sorted(ep.responses)[0]
        r = ep.responses[code]
        return f"{code} {r['type']}" if "type" in r else code

    # ------------------------------------------------------------------ labels / notes
    def _label(self, m: MethodInfo | None, mid: MethodId) -> str:
        if m is not None:
            return m.label
        if "?" in mid.params or not mid.params:
            return f"{mid.name}({'entity' if mid.params else ''})"
        return f"{mid.name}({mid.params})"

    @staticmethod
    def _ret_label(m: MethodInfo | None) -> str:
        if m is None:
            return "result"
        rt = m.return_type
        return str(rt) if str(rt) != "void" else "void"

    def _notes_for(self, m: MethodInfo, alias: str) -> list[Any]:
        bits: list[str] = []
        cls_anns = {a.name: a for a in (m.cls.annotations if m.cls else [])}
        res = [a for a in m.annotations if a.name in _RESILIENCE]
        if res:
            fb = next((a.get("fallbackMethod") for a in res if a.get("fallbackMethod")), None)
            bits.append("Resilience4j: " + ", ".join(a.name for a in res) + (f" (fallback: {fb})" if fb else ""))
        for a in m.annotations:
            if a.name in _CACHE_ANN:
                tgt = a.get("value", "cacheNames")
                bits.append(f"@{a.name}" + (f"({tgt[0] if isinstance(tgt, list) else tgt})" if tgt else ""))
            elif a.name in _SECURITY:
                bits.append(f"@{a.name}" + (f"({_clean(str(a.get('value')), 40)})" if a.get("value") else ""))
            elif a.name == "Async":
                bits.append("@Async")
        if m.ann("Transactional") or "Transactional" in cls_anns:
            bits.append("@Transactional")
        return [Note(alias, "; ".join(bits))] if bits else []

    # ------------------------------------------------------------------ structure (alt / opt / loop)
    def _structure(self, m: MethodInfo) -> list[_SBlock]:
        if m.body is None:
            return []
        key = id(m)
        if key not in self._struct_cache:
            self._struct_cache[key] = _blocks_in(m.body)
        return self._struct_cache[key]

    def _layout(self, events: list[tuple[int, Any]], blocks: list[_SBlock]) -> list[Any]:
        out: list[Any] = []
        emitted: set[int] = set()
        for line, item in events:
            blk = next((b for b in blocks if b.start <= line <= b.end), None)
            if blk is None:
                out.append(item)
                continue
            if id(blk) in emitted:
                continue
            emitted.add(id(blk))
            inside = [(ln, it) for ln, it in events if blk.start <= ln <= blk.end]
            out += self._block_items(blk, inside)
        return out

    def _block_items(self, blk: _SBlock, events: list[tuple[int, Any]]) -> list[Any]:
        branches: list[tuple[str, list[Any]]] = []
        for br in blk.branches:
            evs = [(ln, it) for ln, it in events if br.start <= ln <= br.end]
            if not evs:
                continue
            branches.append((br.label, self._layout(evs, br.blocks)))
        if not branches:  # calls made from the condition expression itself
            return [it for _, it in events]
        if blk.kind == "if":
            if len(branches) == 2:
                return [Block("alt", branches)]
            label, items = branches[0]
            if label == "else" and blk.branches and blk.branches[0].label != "else":
                label = f"not ({blk.branches[0].label})"
            return [Block("opt", [(label, items)])]
        if blk.kind == "loop":
            return [Block("loop", branches)]
        return [Block("opt", branches)]  # catch


@dataclass
class UiEntry:
    label: str
    note: str
