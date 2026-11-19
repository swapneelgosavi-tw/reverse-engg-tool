"""Java call-graph extraction from source (no compiler, no external indexer).

For every method body in the project this resolves `receiver.method(...)` calls to project methods
using the declared types of parameters, fields and locals. Calls to JDK/library types are ignored.
HTTP clients (Feign, RestTemplate, WebClient, RestClient) are recorded as outbound API calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tree_sitter import Node

from .endpoints import _mapping_of, join_paths
from .graph import GraphData, NodeRef, method_id
from .javamodel import ClassInfo, JavaProject, MethodInfo, TypeRef, eval_expr, parse_type

_SPRING_DATA = re.compile(r"(Jpa|Crud|PagingAndSorting|Mongo|Reactive\w*|ListCrud|Elasticsearch|R2dbc)?Repository$")
_SPRING_DATA_RETURNS = {
    "findById": "Optional", "findOne": "Optional", "getById": "E", "getReferenceById": "E", "getOne": "E",
    "save": "E", "saveAndFlush": "E", "findAll": "List", "findAllById": "List", "saveAll": "List",
}
_UNWRAP_FIRST = {"get", "orElseThrow", "orElse", "orElseGet", "getFirst", "getLast", "getBody", "next", "join",
                 "block", "getContent", "peek"}
_SAME_ELEMENT = {"stream": "Stream", "findFirst": "Optional", "findAny": "Optional", "filter": "Stream",
                 "toList": "List", "iterator": "Iterator"}
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}
_REST_TEMPLATE = {"getForObject": "GET", "getForEntity": "GET", "postForObject": "POST", "postForEntity": "POST",
                  "postForLocation": "POST", "put": "PUT", "delete": "DELETE", "patchForObject": "PATCH",
                  "headForHeaders": "HEAD", "optionsForAllow": "OPTIONS"}
_TEMPLATE_TYPES = {"RestTemplate", "TestRestTemplate", "RestOperations"}
_REACTIVE_TYPES = {"WebClient", "RestClient"}


def _t(n: Node | None) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _line(n: Node) -> int:
    return n.start_point.row + 1


@dataclass
class Ty:
    ref: TypeRef
    ctx: ClassInfo | None


class JavaCallIndexer:
    def __init__(self, project: JavaProject, graph: GraphData):
        self.p = project
        self.g = graph
        self._hier: dict[str, list[ClassInfo]] = {}
        self._cls: ClassInfo | None = None
        self._caller = ""
        self._anon = 0

    # ------------------------------------------------------------------ entry
    def run(self) -> None:
        for cls in self.p.classes.values():
            for m in cls.methods:
                self.g.add_node(NodeRef(self._mid(cls, m), "Method", m.name, cls.file, "java"))
        for cls in self.p.classes.values():
            if cls.has_ann("FeignClient"):
                self._feign(cls)
            for m in cls.methods:
                if m.body is not None:
                    self._index_method(cls, m)

    @staticmethod
    def _mid(cls: ClassInfo, m: MethodInfo) -> str:
        return method_id(cls.fqn, m.name, m.signature_key)

    def _index_method(self, cls: ClassInfo, m: MethodInfo) -> None:
        self._cls = cls
        self._caller = self._mid(cls, m)
        self._anon = 0
        env: dict[str, Ty | None] = {p.name: Ty(p.type, cls) for p in m.params}
        assert m.body is not None
        self._walk(m.body, env)

    # ------------------------------------------------------------------ type helpers
    def _hierarchy(self, cls: ClassInfo) -> list[ClassInfo]:
        if cls.fqn in self._hier:
            return self._hier[cls.fqn]
        out = [cls]
        seen = {cls.fqn}
        queue = [cls]
        while queue:
            c = queue.pop(0)
            for s in self.p.supertypes(c):
                if s.fqn not in seen:
                    seen.add(s.fqn)
                    out.append(s)
                    queue.append(s)
        self._hier[cls.fqn] = out
        return out

    def _resolve(self, ty: Ty | None) -> ClassInfo | None:
        return self.p.resolve(ty.ref.name, ty.ctx) if ty is not None and ty.ref.dims == 0 else None

    def _field_ty(self, cls: ClassInfo, name: str) -> Ty | None:
        c: ClassInfo | None = cls
        while c is not None:
            for h in self._hierarchy(c):
                for f in h.fields:
                    if f.name == name:
                        return Ty(f.type, h)
            c = self.p.classes.get(c.outer) if c.outer else None
        return None

    def _find_method(self, cls: ClassInfo, name: str, args: list[Node], env: dict, any_arity: bool = False
                     ) -> tuple[MethodInfo | None, bool]:
        for h in self._hierarchy(cls):
            pool = [m for m in h.methods if m.name == name]
            if not pool:
                continue
            if any_arity:
                return pool[0], len(pool) > 1
            n = len(args)
            fit = [m for m in pool if len(m.params) == n or (m.params and m.params[-1].type.dims and n >= len(m.params) - 1)]
            if not fit:
                continue
            if len(fit) > 1:
                atypes = [self._type_of(a, env) for a in args]
                best = [m for m in fit if all(t is None or self._compatible(t, p.type) for t, p in zip(atypes, m.params))]
                fit = best or fit
            return fit[0], len(fit) > 1
        return None, False

    @staticmethod
    def _compatible(t: Ty, declared: TypeRef) -> bool:
        a, b = t.ref.simple, declared.simple
        boxed = {"int": "Integer", "long": "Long", "double": "Double", "boolean": "Boolean", "float": "Float"}
        return a == b or boxed.get(a) == b or boxed.get(b) == a or b == "Object" or len(b) == 1

    def _type_of(self, n: Node | None, env: dict[str, Ty | None]) -> Ty | None:
        if n is None or self._cls is None:
            return None
        t = n.type
        if t == "identifier":
            name = _t(n)
            if name in env:
                return env[name]
            return self._field_ty(self._cls, name)
        if t == "this":
            return Ty(TypeRef(self._cls.fqn), self._cls)
        if t == "parenthesized_expression" and n.named_children:
            return self._type_of(n.named_children[0], env)
        if t == "cast_expression":
            return Ty(parse_type(n.child_by_field_name("type")), self._cls)
        if t == "object_creation_expression":
            return Ty(parse_type(n.child_by_field_name("type")), self._cls)
        if t == "ternary_expression":
            return self._type_of(n.child_by_field_name("consequence"), env)
        if t == "field_access":
            obj = n.child_by_field_name("object")
            fname = _t(n.child_by_field_name("field"))
            if obj is not None and obj.type == "this":
                return self._field_ty(self._cls, fname)
            oty = self._type_of(obj, env)
            ocls = self._resolve(oty)
            return self._field_ty(ocls, fname) if ocls else None
        if t == "method_invocation":
            return self._invocation_type(n, env)
        return None

    def _static_class(self, n: Node | None, env: dict) -> ClassInfo | None:
        if n is None or self._cls is None or n.type not in ("identifier", "field_access", "scoped_identifier"):
            return None
        text = _t(n)
        head = text.split(".", 1)[0]
        if head in env or self._field_ty(self._cls, head) is not None:
            return None
        return self.p.resolve(text, self._cls)

    def _invocation_type(self, n: Node, env: dict) -> Ty | None:
        obj = n.child_by_field_name("object")
        name = _t(n.child_by_field_name("name"))
        args = n.child_by_field_name("arguments")
        arglist = list(args.named_children) if args else []
        if obj is None:
            cls, owner = self._cls, None
            m, _ = self._find_method(cls, name, arglist, env) if cls else (None, False)
            return Ty(m.return_type, m.cls) if m and m.return_type.name != "void" else None
        rty = Ty(TypeRef(self._cls.fqn), self._cls) if obj.type == "this" and self._cls else self._type_of(obj, env)
        if rty is None:
            sc = self._static_class(obj, env)
            rty = Ty(TypeRef(sc.fqn), sc) if sc else None
        if rty is None:
            return None
        rcls = self._resolve(rty)
        if rcls is not None:
            m, _ = self._find_method(rcls, name, arglist, env)
            if m is not None:
                rt = m.return_type
                if rt.name in (m.cls.type_params if m.cls else []) and m.cls is rcls and rty.ref.args:
                    i = m.cls.type_params.index(rt.name)
                    return Ty(rty.ref.args[i], rty.ctx) if i < len(rty.ref.args) else None
                return Ty(rt, m.cls) if rt.name != "void" else None
            if name in _SPRING_DATA_RETURNS:
                entity = self._repo_entity(rcls)
                if entity is not None:
                    kind = _SPRING_DATA_RETURNS[name]
                    return entity if kind == "E" else Ty(TypeRef(kind, [entity.ref]), entity.ctx)
            return None
        # JDK / library wrappers
        if rty.ref.args:
            first = Ty(rty.ref.args[-1] if rty.ref.simple == "Map" and name == "get" else rty.ref.args[0], rty.ctx)
            if name in _UNWRAP_FIRST:
                return first
            if name in _SAME_ELEMENT:
                return Ty(TypeRef(_SAME_ELEMENT[name], [first.ref]), rty.ctx)
        return None

    def _repo_entity(self, cls: ClassInfo) -> Ty | None:
        for h in self._hierarchy(cls):
            for s in ([h.superclass] if h.superclass else []) + h.interfaces:
                if _SPRING_DATA.search(s.simple) and s.args:
                    return Ty(s.args[0], h)
        return None

    # ------------------------------------------------------------------ walking
    def _walk(self, n: Node, env: dict[str, Ty | None]) -> None:
        t = n.type
        if t == "method_invocation":
            self._invocation(n, env)
            return
        if t == "method_reference":
            self._method_ref(n, env)
            return
        if t == "local_variable_declaration":
            tnode = n.child_by_field_name("type")
            for d in n.children:
                if d.type != "variable_declarator":
                    continue
                v = d.child_by_field_name("value")
                if v is not None:
                    self._walk(v, env)
                name = _t(d.child_by_field_name("name"))
                if tnode is not None and _t(tnode) == "var":
                    env[name] = self._type_of(v, env)
                else:
                    env[name] = Ty(parse_type(tnode), self._cls)
            return
        if t == "lambda_expression":
            inner = dict(env)
            params = n.child_by_field_name("parameters")
            if params is not None:
                names = [params] if params.type == "identifier" else [
                    c for c in params.named_children if c.type in ("identifier", "formal_parameter")]
                for c in names:
                    inner[_t(c.child_by_field_name("name")) if c.type == "formal_parameter" else _t(c)] = None
                    if c.type == "formal_parameter":
                        inner[_t(c.child_by_field_name("name"))] = Ty(parse_type(c.child_by_field_name("type")), self._cls)
            body = n.child_by_field_name("body")
            if body is not None:
                self._walk(body, inner)
            return
        if t == "enhanced_for_statement":
            val = n.child_by_field_name("value")
            if val is not None:
                self._walk(val, env)
            inner = dict(env)
            name = _t(n.child_by_field_name("name"))
            tnode = n.child_by_field_name("type")
            if tnode is not None and _t(tnode) == "var":
                vt = self._type_of(val, env)
                inner[name] = Ty(vt.ref.args[0], vt.ctx) if vt and vt.ref.args else None
            else:
                inner[name] = Ty(parse_type(tnode), self._cls)
            body = n.child_by_field_name("body")
            if body is not None:
                self._walk(body, inner)
            return
        if t == "catch_clause":
            inner = dict(env)
            cp = next((c for c in n.children if c.type == "catch_formal_parameter"), None)
            if cp is not None:
                inner[_t(cp.child_by_field_name("name"))] = None
            for c in n.children:
                if c.type == "block":
                    self._walk(c, inner)
            return
        if t == "try_with_resources_statement":
            inner = dict(env)
            res = n.child_by_field_name("resources")
            for r in (res.named_children if res else []):
                v = r.child_by_field_name("value")
                if v is not None:
                    self._walk(v, inner)
                nm = r.child_by_field_name("name")
                if nm is not None:
                    tn = r.child_by_field_name("type")
                    inner[_t(nm)] = self._type_of(v, inner) if tn is not None and _t(tn) == "var" \
                        else Ty(parse_type(tn), self._cls)
            for c in n.children:
                if c.type in ("block", "catch_clause", "finally_clause"):
                    self._walk(c, inner)
            return
        if t == "instanceof_expression":
            left = n.child_by_field_name("left")
            if left is not None:
                self._walk(left, env)
            name = n.child_by_field_name("name")
            if name is not None:
                env[_t(name)] = Ty(parse_type(n.child_by_field_name("right")), self._cls)
            return
        if t in ("block", "switch_block", "for_statement", "do_statement", "while_statement"):
            env = dict(env)
        if t == "class_body" and n.parent is not None and n.parent.type == "object_creation_expression":
            self._anon += 1
            try:
                for c in n.children:
                    self._walk(c, dict(env))
            finally:
                self._anon -= 1
            return
        if t in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
            return
        for c in n.children:
            self._walk(c, env)

    def _args(self, n: Node) -> list[Node]:
        a = n.child_by_field_name("arguments")
        return [c for c in a.named_children if c.type != "comment"] if a else []

    def _invocation(self, n: Node, env: dict) -> None:
        obj = n.child_by_field_name("object")
        name_node = n.child_by_field_name("name")
        name = _t(name_node)
        args = self._args(n)
        if obj is not None:
            self._walk(obj, env)
        for a in args:
            self._walk(a, env)
        line = _line(name_node) if name_node is not None else _line(n)
        parent = n.parent
        is_chain_top = not (parent is not None and parent.type == "method_invocation"
                            and parent.child_by_field_name("object") == n)
        if is_chain_top:
            self._outbound(n, env)
        self._emit_call(obj, name, args, env, line)

    def _emit_call(self, obj: Node | None, name: str, args: list[Node], env: dict, line: int) -> None:
        cls = self._cls
        if cls is None:
            return
        target_cls: ClassInfo | None = None
        m: MethodInfo | None = None
        ambiguous = False
        if obj is None or obj.type == "this":
            if self._anon:
                return
            c: ClassInfo | None = cls
            while c is not None and m is None:
                m, ambiguous = self._find_method(c, name, args, env)
                if m is not None:
                    target_cls = c
                c = self.p.classes.get(c.outer) if c.outer else None
        elif obj.type == "super":
            sup = self.p.resolve(cls.superclass.name, cls) if cls.superclass else None
            if sup is not None:
                m, ambiguous = self._find_method(sup, name, args, env)
                target_cls = sup if m else None
        else:
            sc = self._static_class(obj, env)
            ty = None if sc else self._type_of(obj, env)
            target_cls = sc or self._resolve(ty)
            if target_cls is None:
                return
            m, ambiguous = self._find_method(target_cls, name, args, env)
        if target_cls is None:
            return
        if m is not None:
            self._edge(target_cls, name, m.signature_key, 0.9 if ambiguous else 0.95, line)
        else:
            self._edge(target_cls, name, ",".join("?" * len(args)), 0.9, line)

    def _edge(self, tcls: ClassInfo, name: str, params: str, conf: float, line: int, reason: str = "resolved") -> None:
        tid = method_id(tcls.fqn, name, params)
        self.g.add_node(NodeRef(tid, "Method", name, tcls.file, "java"))
        self.g.add_call(self._caller, tid, line, conf, reason)

    def _method_ref(self, n: Node, env: dict) -> None:
        kids = [c for c in n.children if c.type != "::"]
        if len(kids) < 2 or self._cls is None:
            return
        target, name = kids[0], _t(kids[-1])
        if name == "new":
            return
        self._walk(target, env)
        if target.type == "this":
            if self._anon:
                return
            tcls: ClassInfo | None = self._cls
        else:
            tcls = self._static_class(target, env) or self._resolve(self._type_of(target, env))
        if tcls is None:
            return
        m, _ = self._find_method(tcls, name, [], env, any_arity=True)
        if m is not None:
            self._edge(tcls, name, m.signature_key, 0.9, _line(n), "method-reference")

    # ------------------------------------------------------------------ outbound HTTP
    def _outbound(self, top: Node, env: dict) -> None:
        links: list[Node] = []
        cur: Node | None = top
        root: Node | None = None
        while cur is not None and cur.type == "method_invocation":
            links.append(cur)
            root = cur.child_by_field_name("object")
            cur = root
        if root is None:
            return
        links.reverse()
        rty = self._type_of(root, env)
        rname = rty.ref.simple if rty else ""
        if not rname:
            rname = _t(root).rsplit(".", 1)[-1] if root.type in ("identifier", "field_access") else ""
        if rname in _TEMPLATE_TYPES and len(links) == 1:
            self._rest_template(links[0], rname, env)
        elif rname in _REACTIVE_TYPES:
            self._reactive_chain(links, rname, env)

    def _rest_template(self, n: Node, client: str, env: dict) -> None:
        name = _t(n.child_by_field_name("name"))
        args = self._args(n)
        if not args:
            return
        method = _REST_TEMPLATE.get(name)
        if name in ("exchange", "execute") and len(args) > 1:
            method = _t(args[1]).rsplit(".", 1)[-1].upper()
        if method is None:
            return
        resp = next((self._class_literal(a) for a in reversed(args) if self._class_literal(a)), None)
        if resp is None:
            ptr = next((a for a in args if a.type == "object_creation_expression"
                        and "ParameterizedTypeReference" in _t(a)), None)
            if ptr is not None:
                resp = str(parse_type(ptr.child_by_field_name("type")).args[0]) if parse_type(
                    ptr.child_by_field_name("type")).args else None
        req = None
        if method in ("POST", "PUT", "PATCH") and len(args) > 1:
            ty = self._type_of(args[1], env)
            req = str(ty.ref) if ty else None
        self.g.add_ext(self._caller, method, self._url(args[0], env), client, _line(n), 0.9, req, resp,
                       language="java")

    def _reactive_chain(self, links: list[Node], client: str, env: dict) -> None:
        method = url = req = resp = None
        line = _line(links[0])
        for ln in links:
            nm = _t(ln.child_by_field_name("name"))
            a = self._args(ln)
            if nm in _HTTP_VERBS and method is None:
                method = nm.upper()
            elif nm == "method" and a:
                method = _t(a[0]).rsplit(".", 1)[-1].upper()
            elif nm == "uri" and a and url is None:
                url = self._url(a[0], env)
                line = _line(ln)
            elif nm in ("bodyValue", "body") and a and req is None and not self._class_literal(a[0]):
                ty = self._type_of(a[0], env)
                req = str(ty.ref) if ty else None
            elif nm in ("bodyToMono", "bodyToFlux", "body", "toEntity", "exchangeToMono", "exchangeToFlux") and a:
                resp = self._class_literal(a[0]) or resp
        if method and url is not None:
            self.g.add_ext(self._caller, method, url, client, line, 0.9, req, resp, language="java")

    @staticmethod
    def _class_literal(n: Node) -> str | None:
        if n.type == "class_literal":
            return _t(n.named_children[0]) if n.named_children else None
        return None

    def _url(self, n: Node, env: dict) -> str:
        return self._eval(n, env) or "{}"

    def _eval(self, n: Node | None, env: dict) -> str:
        if n is None:
            return ""
        t = n.type
        if t == "string_literal":
            return str(eval_expr(n))
        if t == "binary_expression" and _t(n.child_by_field_name("operator")) == "+":
            return self._eval(n.child_by_field_name("left"), env) + self._eval(n.child_by_field_name("right"), env)
        if t == "parenthesized_expression" and n.named_children:
            return self._eval(n.named_children[0], env)
        if t in ("identifier", "field_access") and self._cls is not None:
            name = _t(n).rsplit(".", 1)[-1]
            owner = self._cls
            if t == "field_access":
                sc = self._static_class(n.child_by_field_name("object"), env)
                owner = sc or self._cls
            if t == "identifier" and name in env:
                return "{" + name + "}"
            for h in self._hierarchy(owner) + ([self.p.classes[owner.outer]] if owner.outer else []):
                for f in h.fields:
                    if f.name == name:
                        if f.const_value is not None:
                            return str(f.const_value)
                        v = f.ann("Value")
                        if v is not None and isinstance(v.get("value"), str):
                            return v.get("value")
            return "{" + name + "}"
        if t == "method_invocation" and _t(n.child_by_field_name("name")) == "format":
            args = self._args(n)
            if args and args[0].type == "string_literal":
                return re.sub(r"%[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z]", "{}", str(eval_expr(args[0])))
        return "{" + re.sub(r"\s+", " ", _t(n))[:30] + "}"

    # ------------------------------------------------------------------ Feign
    def _feign(self, cls: ClassInfo) -> None:
        fc = cls.ann("FeignClient")
        assert fc is not None
        url = fc.get("url")
        if not url:
            svc = fc.get("name", "value", "serviceId", default="")
            url = f"http://{svc}" if svc else ""
        rm = cls.ann("RequestMapping")
        cp = rm.get("value", "path", default="") if rm is not None else ""
        prefix = join_paths(str(fc.get("path", default="") or ""), str((cp[0] if isinstance(cp, list) and cp else cp) or ""))
        base = str(url).rstrip("/")
        for m in cls.methods:
            mp = _mapping_of(m.annotations)
            if mp is None:
                continue
            methods, paths, _c, _p = mp
            full = base + join_paths(prefix, paths[0] if paths else "")
            body = next((p for p in m.params if p.ann("RequestBody")), None)
            resp = None if m.return_type.name == "void" else str(m.return_type)
            self.g.add_ext(self._mid(cls, m), (methods or ["GET"])[0], full, "OpenFeign", m.start_line, 0.95,
                           str(body.type) if body else None, resp, language="java")
