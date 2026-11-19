"""React / TypeScript / JavaScript call-graph extraction (tree-sitter, no Node.js required).

Produces function / component / hook nodes, call edges resolved through relative imports, and outbound
HTTP calls made with axios (including `axios.create` instances) or fetch.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_typescript as _tsts
from tree_sitter import Language, Node, Parser

from .graph import GraphData, NodeRef

_TSX = Language(_tsts.language_tsx())
_TS = Language(_tsts.language_typescript())
_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_SKIP_DIRS = {"node_modules", "dist", "build", ".next", "coverage", "out", "target", ".git", ".idea", "__tests__",
              "__mocks__", "cypress", "e2e", ".gradle", "storybook-static"}
_SKIP_FILE = re.compile(r"(\.d\.ts|\.test\.[jt]sx?|\.spec\.[jt]sx?|\.stories\.[jt]sx?)$")
_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
_JSX = {"jsx_element", "jsx_self_closing_element", "jsx_fragment"}
_FUNCS = {"arrow_function", "function_expression", "function", "generator_function"}
_WRAPPERS = {"memo", "forwardRef", "observer", "React.memo", "React.forwardRef", "useCallback", "withRouter",
             "connect"}


def _t(n: Node | None) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _unq(n: Node) -> str:
    return "".join(_t(c) for c in n.children if c.type in ("string_fragment", "escape_sequence"))


@dataclass
class _Mod:
    mid: str  # module id: path without extension, posix style
    path: str
    root: Node
    src: bytes
    defs: dict[str, str] = field(default_factory=dict)  # exported/local name -> node id
    default: str | None = None  # local name that is the default export
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)  # local -> (module id, export | '*')
    reexports: list[tuple[str, str, str]] = field(default_factory=list)  # (local export, module id, remote | '*')
    consts: dict[str, str] = field(default_factory=dict)
    instances: dict[str, str] = field(default_factory=dict)  # axios.create instance -> baseURL
    objects: dict[str, dict[str, str]] = field(default_factory=dict)  # object literal name -> {member: node id}
    raw_imports: list[tuple[str, str, str]] = field(default_factory=list)  # (local, source, export)


class JsCallIndexer:
    def __init__(self, root: Path, graph: GraphData):
        self.root = root.resolve()
        self.g = graph
        self.mods: dict[str, _Mod] = {}
        self._tsx, self._ts = Parser(_TSX), Parser(_TS)
        self._caller = ""
        self._mod: _Mod | None = None
        self._params: dict[str, str] = {}
        self._ret = ""
        self._cls_or_obj: str | None = None

    # ------------------------------------------------------------------ entry
    def run(self) -> int:
        self._scan()
        if not self.mods:
            return 0
        for m in self.mods.values():
            self._collect_imports(m)
        for m in self.mods.values():
            self._collect_consts_and_instances(m)
        for m in self.mods.values():
            self._collect_defs(m)
        for m in self.mods.values():
            self._index_calls(m)
        return len(self.mods)

    def _scan(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if not fn.endswith(_EXTS) or _SKIP_FILE.search(fn):
                    continue
                p = Path(dirpath) / fn
                try:
                    src = p.read_bytes()
                except OSError:
                    continue
                if len(src) > 1_500_000:
                    continue
                rel = p.relative_to(self.root).as_posix()
                mid = os.path.splitext(rel)[0]
                parser = self._ts if fn.endswith(".ts") else self._tsx
                self.mods[mid] = _Mod(mid, rel, parser.parse(src).root_node, src)

    # ------------------------------------------------------------------ module resolution
    def _resolve_module(self, frm: _Mod, source: str) -> str | None:
        if source.startswith("."):
            base = os.path.normpath(os.path.join(os.path.dirname(frm.mid), source)).replace(os.sep, "/")
        elif source.startswith(("@/", "~/")):
            base = None
            for pre in ("src/", ""):
                cand = self._find_mod(pre + source[2:])
                if cand:
                    return cand
            return base
        else:
            for pre in ("src/", ""):
                cand = self._find_mod(pre + source)
                if cand:
                    return cand
            return None
        return self._find_mod(base)

    def _find_mod(self, base: str) -> str | None:
        base = re.sub(r"\.(js|jsx|ts|tsx|mjs|cjs)$", "", base)
        for cand in (base, base + "/index"):
            if cand in self.mods:
                return cand
        return None

    # ------------------------------------------------------------------ pass 1: imports
    def _collect_imports(self, m: _Mod) -> None:
        for n in m.root.children:
            if n.type == "import_statement":
                src = n.child_by_field_name("source")
                if src is None:
                    continue
                target = self._resolve_module(m, _unq(src))
                if target is None:
                    continue
                clause = next((c for c in n.children if c.type == "import_clause"), None)
                for c in (clause.children if clause else []):
                    if c.type == "identifier":
                        m.imports[_t(c)] = (target, "default")
                    elif c.type == "namespace_import":
                        ident = next((x for x in c.children if x.type == "identifier"), None)
                        if ident is not None:
                            m.imports[_t(ident)] = (target, "*")
                    elif c.type == "named_imports":
                        for s in c.children:
                            if s.type == "import_specifier":
                                nm = _t(s.child_by_field_name("name"))
                                al = s.child_by_field_name("alias")
                                m.imports[_t(al) if al is not None else nm] = (target, nm)
            elif n.type == "export_statement" and n.child_by_field_name("source") is not None:
                target = self._resolve_module(m, _unq(n.child_by_field_name("source")))
                if target is None:
                    continue
                clause = next((c for c in n.children if c.type == "export_clause"), None)
                if clause is None:
                    m.reexports.append(("*", target, "*"))
                    continue
                for s in clause.children:
                    if s.type == "export_specifier":
                        nm = _t(s.child_by_field_name("name"))
                        al = s.child_by_field_name("alias")
                        m.reexports.append((_t(al) if al is not None else nm, target, nm))

    # ------------------------------------------------------------------ pass 2: constants, axios instances
    def _declarators(self, m: _Mod):
        for n in m.root.children:
            decl = n.child_by_field_name("declaration") if n.type == "export_statement" else n
            if decl is not None and decl.type in ("lexical_declaration", "variable_declaration"):
                for d in decl.children:
                    if d.type == "variable_declarator":
                        yield d

    def _collect_consts_and_instances(self, m: _Mod) -> None:
        for d in self._declarators(m):
            name = _t(d.child_by_field_name("name"))
            v = d.child_by_field_name("value")
            if v is None:
                continue
            if v.type == "string" or (v.type == "template_string" and not any(
                    c.type == "template_substitution" for c in v.children)):
                m.consts[name] = _unq(v)
            elif v.type in ("template_string", "binary_expression"):
                m.consts[name] = self._eval(v, m, {})

        for d in self._declarators(m):
            name = _t(d.child_by_field_name("name"))
            v = d.child_by_field_name("value")
            if v is not None and v.type == "call_expression" and _t(v.child_by_field_name("function")) == "axios.create":
                base = ""
                a = v.child_by_field_name("arguments")
                cfg = a.named_children[0] if a and a.named_children else None
                if cfg is not None and cfg.type == "object":
                    for p in cfg.children:
                        if p.type == "pair" and _t(p.child_by_field_name("key")) == "baseURL":
                            base = self._eval(p.child_by_field_name("value"), m, {})
                m.instances[name] = base

    # ------------------------------------------------------------------ pass 3: definitions
    def _is_component(self, name: str, body: Node | None) -> bool:
        return bool(name[:1].isupper()) and body is not None and self._has_jsx(body)

    @staticmethod
    def _has_jsx(n: Node) -> bool:
        stack = [n]
        while stack:
            x = stack.pop()
            if x.type in _JSX:
                return True
            stack.extend(x.children)
        return False

    def _kind(self, name: str, body: Node | None) -> str:
        if re.match(r"use[A-Z0-9]", name):
            return "Hook"
        return "Component" if self._is_component(name.split(".")[0], body) else "Function"

    def _add(self, m: _Mod, name: str, body: Node | None, local: str | None = None) -> str:
        nid = f"function:{m.mid}.{name}"
        self.g.add_node(NodeRef(nid, self._kind(name.split(".")[-1], body), name, m.path, "typescript"))
        m.defs[local or name] = nid
        return nid

    def _unwrap(self, v: Node | None) -> Node | None:
        while v is not None and v.type == "call_expression" and _t(v.child_by_field_name("function")) in _WRAPPERS:
            a = v.child_by_field_name("arguments")
            v = a.named_children[0] if a and a.named_children else None
        while v is not None and v.type in ("parenthesized_expression", "as_expression", "satisfies_expression"):
            v = v.named_children[0] if v.named_children else None
        return v

    def _collect_defs(self, m: _Mod) -> None:
        for n in m.root.children:
            is_default = False
            decl = n
            if n.type == "export_statement":
                is_default = any(c.type == "default" for c in n.children)
                decl = n.child_by_field_name("declaration")
                if decl is None:
                    val = n.child_by_field_name("value")
                    if is_default and val is not None:
                        if val.type == "identifier":
                            m.default = _t(val)
                        else:
                            inner = self._unwrap(val)
                            if inner is not None and inner.type in _FUNCS:
                                m.default = "default"
                                self._add(m, "default", inner.child_by_field_name("body"))
                    elif n.child_by_field_name("source") is None:
                        clause = next((c for c in n.children if c.type == "export_clause"), None)
                        for s in (clause.children if clause else []):
                            if s.type == "export_specifier":
                                nm = _t(s.child_by_field_name("name"))
                                al = s.child_by_field_name("alias")
                                if al is not None and _t(al) == "default":
                                    m.default = nm
                                elif al is not None:
                                    m.reexports.append((_t(al), m.mid, nm))
                    continue
            if decl is None:
                continue
            if decl.type in ("function_declaration", "generator_function_declaration"):
                nm = _t(decl.child_by_field_name("name")) or "default"
                self._add(m, nm, decl.child_by_field_name("body"))
                if is_default:
                    m.default = nm
            elif decl.type in ("class_declaration", "abstract_class_declaration"):
                nm = _t(decl.child_by_field_name("name"))
                body = decl.child_by_field_name("body")
                heritage = _t(next((c for c in decl.children if c.type == "class_heritage"), None))
                if re.search(r"\b(React\.)?(Pure)?Component\b", heritage) and nm[:1].isupper():
                    nid = f"function:{m.mid}.{nm}"
                    self.g.add_node(NodeRef(nid, "Component", nm, m.path, "typescript"))
                    m.defs[nm] = nid
                else:
                    methods: dict[str, str] = {}
                    for c in (body.children if body else []):
                        if c.type == "method_definition":
                            mn = _t(c.child_by_field_name("name"))
                            methods[mn] = self._add(m, f"{nm}.{mn}", c.child_by_field_name("body"), f"{nm}.{mn}")
                    m.objects[nm] = methods
                if is_default:
                    m.default = nm
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                for d in decl.children:
                    if d.type != "variable_declarator":
                        continue
                    nm = _t(d.child_by_field_name("name"))
                    v = self._unwrap(d.child_by_field_name("value"))
                    if v is None or d.child_by_field_name("name").type != "identifier":
                        continue
                    if v.type in _FUNCS:
                        self._add(m, nm, v.child_by_field_name("body"))
                    elif v.type == "object":
                        members: dict[str, str] = {}
                        for c in v.children:
                            if c.type == "pair":
                                fn = self._unwrap(c.child_by_field_name("value"))
                                if fn is not None and fn.type in _FUNCS:
                                    mn = _t(c.child_by_field_name("key"))
                                    members[mn] = self._add(m, f"{nm}.{mn}", fn.child_by_field_name("body"), f"{nm}.{mn}")
                            elif c.type == "method_definition":
                                mn = _t(c.child_by_field_name("name"))
                                members[mn] = self._add(m, f"{nm}.{mn}", c.child_by_field_name("body"), f"{nm}.{mn}")
                        if members:
                            m.objects[nm] = members
                    if is_default:
                        m.default = nm

    # ------------------------------------------------------------------ lookups across modules
    def _export_of(self, mid: str, name: str, depth: int = 0) -> str | None:
        """Node id for export `name` of module `mid` (follows re-exports)."""
        mod = self.mods.get(mid)
        if mod is None or depth > 6:
            return None
        if name == "default":
            return mod.defs.get(mod.default or "")
        if name in mod.defs:
            return mod.defs[name]
        for local, target, remote in mod.reexports:
            if local == name:
                return self._export_of(target, remote if remote != "*" else name, depth + 1)
        for local, target, _r in mod.reexports:
            if local == "*":
                hit = self._export_of(target, name, depth + 1)
                if hit:
                    return hit
        return None

    def _object_of(self, mid: str, name: str, depth: int = 0) -> dict[str, str] | None:
        mod = self.mods.get(mid)
        if mod is None or depth > 6:
            return None
        if name == "default":
            name = mod.default or ""
        if name in mod.objects:
            return mod.objects[name]
        for local, target, remote in mod.reexports:
            if local == name:
                return self._object_of(target, remote if remote != "*" else name, depth + 1)
            if local == "*":
                hit = self._object_of(target, name, depth + 1)
                if hit:
                    return hit
        return None

    def _instance_base(self, m: _Mod, name: str) -> str | None:
        if name in m.instances:
            return m.instances[name]
        imp = m.imports.get(name)
        if imp:
            tm = self.mods.get(imp[0])
            if tm is not None:
                key = imp[1] if imp[1] != "default" else (tm.default or "")
                if key in tm.instances:
                    return tm.instances[key]
                for local, target, remote in tm.reexports:
                    if local == key and target in self.mods and remote in self.mods[target].instances:
                        return self.mods[target].instances[remote]
        return None

    def _const(self, m: _Mod, name: str) -> str | None:
        if name in m.consts:
            return m.consts[name]
        imp = m.imports.get(name)
        if imp and imp[0] in self.mods:
            return self.mods[imp[0]].consts.get(imp[1])
        return None

    # ------------------------------------------------------------------ URL evaluation
    def _eval(self, n: Node | None, m: _Mod, params: dict[str, str]) -> str:
        if n is None:
            return ""
        t = n.type
        if t == "string":
            return _unq(n)
        if t == "template_string":
            out = []
            for c in n.children:
                if c.type == "template_substitution":
                    out.append(self._eval(c.named_children[0] if c.named_children else None, m, params))
                elif c.type in ("string_fragment", "escape_sequence"):
                    out.append(_t(c))
            return "".join(out)
        if t == "binary_expression" and _t(n.child_by_field_name("operator")) == "+":
            return self._eval(n.child_by_field_name("left"), m, params) + self._eval(n.child_by_field_name("right"), m, params)
        if t == "parenthesized_expression" and n.named_children:
            return self._eval(n.named_children[0], m, params)
        if t in ("as_expression", "non_null_expression") and n.named_children:
            return self._eval(n.named_children[0], m, params)
        if t == "identifier":
            c = self._const(m, _t(n))
            return c if c is not None else "{" + _t(n) + "}"
        text = _t(n)
        if re.match(r"(process\.env|import\.meta\.env)\b", text):
            return ""
        if t == "member_expression":
            return "{" + _t(n.child_by_field_name("property")) + "}"
        if t == "call_expression" and _t(n.child_by_field_name("function")).endswith(".toString"):
            return self._eval(n.child_by_field_name("function").child_by_field_name("object"), m, params)
        return "{" + re.sub(r"\s+", " ", text)[:24] + "}"

    @staticmethod
    def _clean_url(url: str) -> str:
        url = re.sub(r"^\{[^}]*\}(?=/)", "", url)  # leading ${BASE} placeholder
        return url if url.startswith(("http://", "https://", "/", "{")) or not url else "/" + url

    # ------------------------------------------------------------------ pass 4: calls
    def _index_calls(self, m: _Mod) -> None:
        self._mod = m
        for n in m.root.children:
            decl = n.child_by_field_name("declaration") if n.type == "export_statement" else n
            val = n.child_by_field_name("value") if n.type == "export_statement" and decl is None else None
            if val is not None:
                inner = self._unwrap(val)
                if inner is not None and inner.type in _FUNCS and "default" in m.defs:
                    self._body(m.defs["default"], inner)
                continue
            if decl is None:
                continue
            if decl.type in ("function_declaration", "generator_function_declaration"):
                nid = m.defs.get(_t(decl.child_by_field_name("name")) or "default")
                if nid:
                    self._body(nid, decl)
            elif decl.type in ("class_declaration", "abstract_class_declaration"):
                nm = _t(decl.child_by_field_name("name"))
                body = decl.child_by_field_name("body")
                if m.defs.get(nm) and nm not in m.objects:  # class component: all methods belong to it
                    self._body(m.defs[nm], body)
                else:
                    for c in (body.children if body else []):
                        if c.type == "method_definition":
                            nid = m.objects.get(nm, {}).get(_t(c.child_by_field_name("name")))
                            if nid:
                                self._body(nid, c, cls=nm)
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                for d in decl.children:
                    if d.type != "variable_declarator":
                        continue
                    nm = _t(d.child_by_field_name("name"))
                    v = self._unwrap(d.child_by_field_name("value"))
                    if v is None:
                        continue
                    if v.type in _FUNCS and nm in m.defs:
                        self._body(m.defs[nm], v)
                    elif v.type == "object" and nm in m.objects:
                        for c in v.children:
                            if c.type == "pair":
                                fn = self._unwrap(c.child_by_field_name("value"))
                                nid = m.objects[nm].get(_t(c.child_by_field_name("key")))
                                if nid and fn is not None and fn.type in _FUNCS:
                                    self._body(nid, fn, obj=nm)
                            elif c.type == "method_definition":
                                nid = m.objects[nm].get(_t(c.child_by_field_name("name")))
                                if nid:
                                    self._body(nid, c, obj=nm)

    def _body(self, nid: str, fn: Node, cls: str | None = None, obj: str | None = None) -> None:
        self._caller = nid
        self._params = {}
        params = fn.child_by_field_name("parameters")
        for p in (params.named_children if params else []):
            pat = p.child_by_field_name("pattern")
            ann = next((c for c in p.children if c.type == "type_annotation"), None)
            if pat is not None and ann is not None:
                self._params[_t(pat)] = _t(ann).lstrip(": ").strip()
        ret = next((c for c in fn.children if c.type == "type_annotation"), None)
        self._ret = _t(ret).lstrip(": ").strip() if ret is not None else ""
        self._cls_or_obj = cls or obj
        body = fn.child_by_field_name("body") or fn
        self._walk(body)

    def _walk(self, n: Node) -> None:
        for c in n.children:
            self._walk(c)
        if n.type == "call_expression":
            self._call(n)

    def _call(self, n: Node) -> None:
        m = self._mod
        assert m is not None
        fn = n.child_by_field_name("function")
        if fn is None:
            return
        line = n.start_point.row + 1
        if fn.type == "identifier":
            name = _t(fn)
            if name == "fetch":
                self._fetch(n, m, line)
                return
            if name == "axios":
                self._axios_config(n, m, line)
                return
            self._edge(self._lookup(m, name), line)
        elif fn.type == "member_expression":
            obj, prop = fn.child_by_field_name("object"), _t(fn.child_by_field_name("property"))
            if obj is None:
                return
            if obj.type == "identifier":
                oname = _t(obj)
                if oname == "axios" and prop in _VERBS:
                    self._axios(n, m, prop, "", line)
                    return
                base = self._instance_base(m, oname)
                if base is not None and prop in _VERBS | {"request"}:
                    self._axios(n, m, prop, base, line)
                    return
                if oname in m.objects and f"{oname}.{prop}" in m.defs:
                    self._edge(m.defs[f"{oname}.{prop}"], line)
                    return
                imp = m.imports.get(oname)
                if imp:
                    if imp[1] == "*":
                        self._edge(self._export_of(imp[0], prop), line)
                    else:
                        members = self._object_of(imp[0], imp[1])
                        if members and prop in members:
                            self._edge(members[prop], line)
            elif obj.type == "this" and self._cls_or_obj:
                self._edge(m.defs.get(f"{self._cls_or_obj}.{prop}"), line)

    def _lookup(self, m: _Mod, name: str) -> str | None:
        if name in m.defs:
            return m.defs[name]
        imp = m.imports.get(name)
        if imp and imp[1] != "*":
            return self._export_of(imp[0], imp[1])
        return None

    def _edge(self, target: str | None, line: int) -> None:
        if target and target != self._caller:
            self.g.add_call(self._caller, target, line, 0.95, "import-resolved")

    # ------------------------------------------------------------------ outbound
    def _types(self, n: Node, body_arg: Node | None) -> tuple[str | None, str | None]:
        resp = None
        ta = n.child_by_field_name("type_arguments")
        if ta is not None and ta.named_children:
            resp = _t(ta.named_children[0])
        elif self._ret:
            mm = re.match(r"Promise<(.+)>$", self._ret)
            resp = mm.group(1) if mm else None
        if resp:
            mm = re.match(r"AxiosResponse<(.+?)(?:,.*)?>$", resp)
            resp = mm.group(1) if mm else resp
        req = None
        if body_arg is not None and body_arg.type == "identifier":
            req = self._params.get(_t(body_arg))
        return req, resp

    def _query(self, cfg: Node | None) -> str | None:
        if cfg is None or cfg.type != "object":
            return None
        for p in cfg.children:
            key = p.child_by_field_name("key") if p.type == "pair" else None
            if key is not None and _t(key) == "params":
                v = p.child_by_field_name("value")
                if v is not None and v.type == "object":
                    names = []
                    for c in v.children:
                        if c.type == "shorthand_property_identifier":
                            names.append(_t(c))
                        elif c.type == "pair":
                            names.append(_t(c.child_by_field_name("key")).strip("'\""))
                    return ",".join(names) or None
        return None

    def _axios(self, n: Node, m: _Mod, verb: str, base: str, line: int) -> None:
        a = n.child_by_field_name("arguments")
        args = [c for c in a.named_children if c.type != "comment"] if a else []
        if verb == "request" and args and args[0].type == "object":
            self._axios_config_obj(n, args[0], m, base, line)
            return
        if not args:
            return
        url = self._join(base, self._eval(args[0], m, {}))
        has_body = verb in ("post", "put", "patch")
        req, resp = self._types(n, args[1] if has_body and len(args) > 1 else None)
        cfg = args[2] if has_body and len(args) > 2 else (args[1] if not has_body and len(args) > 1 else None)
        self.g.add_ext(self._caller, verb.upper(), self._clean_url(url), "axios", line, 0.9, req, resp,
                       self._query(cfg), "typescript")

    def _axios_config(self, n: Node, m: _Mod, line: int) -> None:
        a = n.child_by_field_name("arguments")
        if a and a.named_children and a.named_children[0].type == "object":
            self._axios_config_obj(n, a.named_children[0], m, "", line)

    def _axios_config_obj(self, n: Node, cfg: Node, m: _Mod, base: str, line: int) -> None:
        url = method = ""
        data = None
        for p in cfg.children:
            if p.type != "pair":
                continue
            k = _t(p.child_by_field_name("key"))
            v = p.child_by_field_name("value")
            if k == "url":
                url = self._eval(v, m, {})
            elif k == "method":
                method = self._eval(v, m, {}).upper()
            elif k == "data":
                data = v
        if url:
            req, resp = self._types(n, data)
            self.g.add_ext(self._caller, method or "GET", self._clean_url(self._join(base, url)), "axios", line, 0.9,
                           req, resp, self._query(cfg), "typescript")

    def _fetch(self, n: Node, m: _Mod, line: int) -> None:
        a = n.child_by_field_name("arguments")
        args = [c for c in a.named_children if c.type != "comment"] if a else []
        if not args:
            return
        url = self._clean_url(self._eval(args[0], m, {}))
        method, body = "GET", None
        if len(args) > 1 and args[1].type == "object":
            for p in args[1].children:
                if p.type == "pair":
                    k = _t(p.child_by_field_name("key"))
                    if k == "method":
                        method = self._eval(p.child_by_field_name("value"), m, {}).upper() or "GET"
                    elif k == "body":
                        body = p.child_by_field_name("value")
        if body is not None and body.type == "call_expression" and _t(body.child_by_field_name("function")) == "JSON.stringify":
            ba = body.child_by_field_name("arguments")
            body = ba.named_children[0] if ba and ba.named_children else None
        req, resp = self._types(n, body)
        self.g.add_ext(self._caller, method, url, "fetch", line, 0.9, req, resp, None, "typescript")

    @staticmethod
    def _join(base: str, url: str) -> str:
        if not base or url.startswith(("http://", "https://")):
            return url
        return base.rstrip("/") + "/" + url.lstrip("/")
