"""Source-level Java model built with tree-sitter.

The Ladybug graph (from the reverselens indexer) gives resolved call edges, but it does not keep
parameter annotations, annotation arrays, generic field types or Javadoc. This module recovers
those facts straight from the source so request/response contracts can be built exactly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tree_sitter_java as _tsj
from tree_sitter import Language, Node, Parser

_LANG = Language(_tsj.language())

SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "dist", ".gradle", ".idea"}


# --------------------------------------------------------------------------- model


@dataclass
class TypeRef:
    name: str
    args: list["TypeRef"] = field(default_factory=list)
    dims: int = 0

    @property
    def simple(self) -> str:
        return self.name.rsplit(".", 1)[-1]

    def __str__(self) -> str:
        s = self.name
        if self.args:
            s += "<" + ",".join(str(a) for a in self.args) + ">"
        return s + "[]" * self.dims


@dataclass
class Annotation:
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    def get(self, *keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in self.args:
                return self.args[k]
        return default


@dataclass
class FieldInfo:
    name: str
    type: TypeRef
    annotations: list[Annotation] = field(default_factory=list)
    modifiers: set[str] = field(default_factory=set)
    const_value: Any = None
    doc: str = ""
    line: int = 0

    def ann(self, name: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name == name), None)


@dataclass
class ParamInfo:
    name: str
    type: TypeRef
    annotations: list[Annotation] = field(default_factory=list)

    def ann(self, *names: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name in names), None)


@dataclass
class MethodInfo:
    name: str
    params: list[ParamInfo]
    return_type: TypeRef
    annotations: list[Annotation]
    modifiers: set[str]
    start_line: int
    end_line: int
    body: Node | None
    doc: str
    param_docs: dict[str, str]
    cls: "ClassInfo | None" = None

    def ann(self, *names: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name in names), None)

    @property
    def signature_key(self) -> str:
        return ",".join(str(p.type) for p in self.params)

    @property
    def label(self) -> str:
        return f"{self.name}({', '.join(p.type.simple + ('[]' * p.type.dims) for p in self.params)})"

    def body_text(self) -> str:
        return self.body.text.decode("utf-8", "replace") if self.body is not None else ""


@dataclass
class ClassInfo:
    fqn: str
    name: str
    package: str
    kind: str  # class | interface | enum | record
    annotations: list[Annotation]
    superclass: TypeRef | None
    interfaces: list[TypeRef]
    type_params: list[str]
    fields: list[FieldInfo]
    methods: list[MethodInfo]
    enum_constants: list[str]
    imports: list[str]
    file: str
    module: str
    doc: str = ""
    outer: str | None = None

    def ann(self, *names: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name in names), None)

    def has_ann(self, *names: str) -> bool:
        return self.ann(*names) is not None


# --------------------------------------------------------------------------- helpers


def _text(n: Node | None) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _unquote(s: str) -> str:
    if s.startswith('"""'):
        return s[3:-3].strip()
    s = s[1:-1] if len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s
    return s.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")


def eval_expr(n: Node, consts: dict[str, Any] | None = None) -> Any:
    """Evaluate a literal-ish annotation argument. Unknown expressions fall back to their text."""
    consts = consts or {}
    t = n.type
    if t == "string_literal":
        return _unquote(_text(n))
    if t == "element_value_array_initializer":
        return [eval_expr(c, consts) for c in n.named_children if c.type != "comment"]
    if t == "parenthesized_expression" and n.named_children:
        return eval_expr(n.named_children[0], consts)
    if t == "binary_expression" and _text(n.child_by_field_name("operator")) == "+":
        left = eval_expr(n.child_by_field_name("left"), consts)
        right = eval_expr(n.child_by_field_name("right"), consts)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        return f"{left}{right}"
    if t in ("decimal_integer_literal", "hex_integer_literal"):
        try:
            return int(_text(n).rstrip("lL").replace("_", ""), 0)
        except ValueError:
            return _text(n)
    if t == "decimal_floating_point_literal":
        try:
            return float(_text(n).rstrip("dDfF").replace("_", ""))
        except ValueError:
            return _text(n)
    if t == "true":
        return True
    if t == "false":
        return False
    if t == "unary_expression":
        inner = eval_expr(n.named_children[-1], consts)
        if isinstance(inner, (int, float)) and _text(n).startswith("-"):
            return -inner
        return _text(n)
    if t in ("identifier", "field_access", "scoped_identifier"):
        txt = _text(n)
        key = txt.rsplit(".", 1)[-1]
        if txt in consts:
            return consts[txt]
        if key in consts and not txt.startswith(("HttpStatus", "RequestMethod", "MediaType", "HttpMethod")):
            return consts[key]
        return txt
    return _text(n)


def parse_annotation(n: Node, consts: dict[str, Any]) -> Annotation:
    name = _text(n.child_by_field_name("name"))
    args: dict[str, Any] = {}
    arglist = n.child_by_field_name("arguments")
    if arglist is not None:
        for c in arglist.named_children:
            if c.type == "element_value_pair":
                k = _text(c.child_by_field_name("key"))
                v = c.child_by_field_name("value")
                args[k] = eval_expr(v, consts) if v is not None else None
            elif c.type != "comment":
                args["value"] = eval_expr(c, consts)
    return Annotation(name.rsplit(".", 1)[-1], args)


def parse_modifiers(n: Node | None, consts: dict[str, Any]) -> tuple[list[Annotation], set[str]]:
    anns: list[Annotation] = []
    mods: set[str] = set()
    if n is None:
        return anns, mods
    for c in n.children:
        if c.type in ("annotation", "marker_annotation"):
            anns.append(parse_annotation(c, consts))
        elif c.type not in ("comment",):
            mods.add(_text(c))
    return anns, mods


def parse_type(n: Node | None) -> TypeRef:
    if n is None:
        return TypeRef("void")
    t = n.type
    if t == "generic_type":
        base = next((c for c in n.children if c.type in ("type_identifier", "scoped_type_identifier")), None)
        targs = next((c for c in n.children if c.type == "type_arguments"), None)
        args = [parse_type(c) for c in (targs.named_children if targs else []) if c.type != "comment"]
        return TypeRef(_text(base), args)
    if t == "array_type":
        inner = parse_type(n.child_by_field_name("element"))
        dims = n.child_by_field_name("dimensions")
        inner.dims += max(1, _text(dims).count("[")) if dims is not None else 1
        return inner
    if t == "wildcard":
        bound = next((c for c in n.named_children), None)
        return parse_type(bound) if bound is not None else TypeRef("Object")
    return TypeRef(_text(n))


def _doc_before(n: Node) -> tuple[str, dict[str, str]]:
    prev = n.prev_sibling
    while prev is not None and prev.type in ("line_comment",):
        prev = prev.prev_sibling
    if prev is None or prev.type != "block_comment" or not _text(prev).startswith("/**"):
        return "", {}
    raw = _text(prev)[3:-2]
    lines = [re.sub(r"^\s*\*\s?", "", ln).rstrip() for ln in raw.splitlines()]
    summary: list[str] = []
    params: dict[str, str] = {}
    for ln in lines:
        s = ln.strip()
        if s.startswith("@param"):
            parts = s.split(None, 2)
            if len(parts) >= 2:
                params[parts[1]] = parts[2] if len(parts) > 2 else ""
        elif s.startswith("@"):
            continue
        elif not params and s:
            summary.append(s)
    text = re.sub(r"<[^>]+>", "", " ".join(summary)).strip()
    return text, params


# --------------------------------------------------------------------------- parsing


class JavaProject:
    """All Java types found under a repository, with lookup helpers."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.classes: dict[str, ClassInfo] = {}
        self.by_simple: dict[str, list[ClassInfo]] = {}
        self._parser = Parser(_LANG)
        self.parse_errors: list[str] = []
        self._module_cache: dict[str, str] = {}
        self._scan()

    # -- discovery
    def _module_of(self, path: Path) -> str:
        d = path.parent
        while True:
            key = str(d)
            if key in self._module_cache:
                return self._module_cache[key]
            if any((d / f).exists() for f in ("pom.xml", "build.gradle", "build.gradle.kts")) or d == self.root:
                self._module_cache[key] = str(d)
                return str(d)
            if d.parent == d:
                return str(self.root)
            d = d.parent

    def _scan(self) -> None:
        consts_global: dict[str, Any] = {}
        parsed: list[tuple[Path, bytes, Node]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            in_src = os.path.basename(dirpath) == "src"
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                           and not (in_src and d in ("test", "testFixtures", "integrationTest"))]
            for fn in filenames:
                if not fn.endswith(".java") or fn in ("package-info.java", "module-info.java"):
                    continue
                p = Path(dirpath) / fn
                try:
                    src = p.read_bytes()
                    tree = self._parser.parse(src)
                except OSError as e:  # pragma: no cover
                    self.parse_errors.append(f"{p}: {e}")
                    continue
                parsed.append((p, src, tree.root_node))
                self._collect_constants(tree.root_node, consts_global)
        for p, _src, root in parsed:
            self._parse_file(p, root, consts_global)
        for c in self.classes.values():
            self.by_simple.setdefault(c.name, []).append(c)

    def _collect_constants(self, root: Node, out: dict[str, Any]) -> None:
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "field_declaration":
                mods = _text(next((c for c in n.children if c.type == "modifiers"), None))
                if "static" in mods and "final" in mods:
                    for d in n.children:
                        if d.type == "variable_declarator":
                            v = d.child_by_field_name("value")
                            if v is not None and v.type == "string_literal":
                                out.setdefault(_text(d.child_by_field_name("name")), _unquote(_text(v)))
            else:
                stack.extend(n.children)

    def _parse_file(self, path: Path, root: Node, consts: dict[str, Any]) -> None:
        package = ""
        imports: list[str] = []
        for c in root.children:
            if c.type == "package_declaration":
                package = _text(next((x for x in c.named_children if x.type in ("scoped_identifier", "identifier")), None))
            elif c.type == "import_declaration":
                txt = _text(c).replace("import", "", 1).replace(";", "").strip()
                if not txt.startswith("static "):
                    imports.append(txt)
        module = self._module_of(path)
        for c in root.children:
            if c.type in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
                self._parse_type(c, package, imports, str(path), module, consts, None)

    def _parse_type(self, n: Node, package: str, imports: list[str], file: str, module: str,
                    consts: dict[str, Any], outer: ClassInfo | None) -> None:
        kind = {"class_declaration": "class", "interface_declaration": "interface",
                "enum_declaration": "enum", "record_declaration": "record"}[n.type]
        name = _text(n.child_by_field_name("name"))
        fqn = f"{outer.fqn}.{name}" if outer else (f"{package}.{name}" if package else name)
        mod = next((c for c in n.children if c.type == "modifiers"), None)
        anns, _ = parse_modifiers(mod, consts)
        superclass = None
        sc = n.child_by_field_name("superclass")
        if sc is not None:
            superclass = parse_type(next((c for c in sc.named_children), None))
        interfaces: list[TypeRef] = []
        for key in ("interfaces", "extends_interfaces"):
            node = n.child_by_field_name(key)
            if node is not None:
                tl = next((c for c in node.named_children if c.type == "type_list"), node)
                interfaces += [parse_type(c) for c in tl.named_children if c.type != "comment"]
        for c in n.children:
            if c.type == "extends_interfaces":
                tl = next((x for x in c.named_children if x.type == "type_list"), c)
                interfaces += [parse_type(x) for x in tl.named_children if x.type != "comment"]
        tp = n.child_by_field_name("type_parameters")
        type_params = [_text(next((x for x in t.named_children if x.type == "type_identifier"), None))
                       for t in (tp.named_children if tp else []) if t.type == "type_parameter"]
        doc, _ = _doc_before(n)
        info = ClassInfo(fqn=fqn, name=name, package=package, kind=kind, annotations=anns,
                         superclass=superclass, interfaces=interfaces, type_params=type_params,
                         fields=[], methods=[], enum_constants=[], imports=imports, file=file,
                         module=module, doc=doc, outer=outer.fqn if outer else None)
        self.classes[fqn] = info

        if kind == "record":
            params = n.child_by_field_name("parameters")
            for p in (params.named_children if params else []):
                if p.type == "formal_parameter":
                    pa, _ = parse_modifiers(next((c for c in p.children if c.type == "modifiers"), None), consts)
                    info.fields.append(FieldInfo(_text(p.child_by_field_name("name")),
                                                 parse_type(p.child_by_field_name("type")), pa))
        body = n.child_by_field_name("body")
        if body is None:
            return
        for m in body.children:
            if m.type == "field_declaration":
                self._parse_field(m, info, consts)
            elif m.type in ("method_declaration", "constructor_declaration"):
                if m.type == "method_declaration":
                    info.methods.append(self._parse_method(m, info, consts))
            elif m.type in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
                self._parse_type(m, package, imports, file, module, consts, info)
            elif m.type == "enum_body_declarations":
                for mm in m.children:
                    if mm.type == "field_declaration":
                        self._parse_field(mm, info, consts)
                    elif mm.type == "method_declaration":
                        info.methods.append(self._parse_method(mm, info, consts))
            elif m.type == "enum_constant":
                info.enum_constants.append(_text(m.child_by_field_name("name")))

    def _parse_field(self, n: Node, info: ClassInfo, consts: dict[str, Any]) -> None:
        anns, mods = parse_modifiers(next((c for c in n.children if c.type == "modifiers"), None), consts)
        ftype = parse_type(n.child_by_field_name("type"))
        doc, _ = _doc_before(n)
        if info.kind == "interface":
            mods |= {"static", "final"}
        for d in n.children:
            if d.type != "variable_declarator":
                continue
            const = None
            v = d.child_by_field_name("value")
            if v is not None and v.type == "string_literal":
                const = _unquote(_text(v))
            vtype = ftype
            dims = d.child_by_field_name("dimensions")
            if dims is not None:
                vtype = TypeRef(ftype.name, ftype.args, ftype.dims + max(1, _text(dims).count("[")))
            info.fields.append(FieldInfo(_text(d.child_by_field_name("name")), vtype, anns, mods, const,
                                         doc, d.start_point.row + 1))

    def _parse_method(self, n: Node, info: ClassInfo, consts: dict[str, Any]) -> MethodInfo:
        anns, mods = parse_modifiers(next((c for c in n.children if c.type == "modifiers"), None), consts)
        params: list[ParamInfo] = []
        fp = n.child_by_field_name("parameters")
        for p in (fp.named_children if fp else []):
            if p.type in ("formal_parameter", "spread_parameter"):
                pa, _ = parse_modifiers(next((c for c in p.children if c.type == "modifiers"), None), consts)
                if p.type == "spread_parameter":
                    pname = _text(next((c.child_by_field_name("name") for c in p.children
                                        if c.type == "variable_declarator"), None))
                    ptype = parse_type(next((c for c in p.named_children
                                             if c.type not in ("modifiers", "variable_declarator")), None))
                    ptype.dims += 1
                else:
                    pname = _text(p.child_by_field_name("name"))
                    ptype = parse_type(p.child_by_field_name("type"))
                params.append(ParamInfo(pname, ptype, pa))
        doc, pdocs = _doc_before(n)
        if info.kind == "interface":
            mods.add("public")
        return MethodInfo(
            name=_text(n.child_by_field_name("name")), params=params,
            return_type=parse_type(n.child_by_field_name("type")), annotations=anns, modifiers=mods,
            start_line=n.start_point.row + 1, end_line=n.end_point.row + 1,
            body=n.child_by_field_name("body"), doc=doc, param_docs=pdocs, cls=info,
        )

    # -- lookup
    def resolve(self, tname: str, ctx: ClassInfo | None) -> ClassInfo | None:
        """Resolve a type name as written in `ctx` to a project class (None for JDK/library types)."""
        if not tname:
            return None
        tname = tname.split("<", 1)[0]
        if tname in self.classes:
            return self.classes[tname]
        simple = tname.rsplit(".", 1)[-1]
        cands = self.by_simple.get(simple, [])
        if not cands:
            return None
        if ctx is not None:
            for imp in ctx.imports:
                if imp == tname or imp.endswith("." + tname):
                    hit = self.classes.get(imp) or self.classes.get(imp.rsplit(".", 1)[0] + "." + tname)
                    if hit:
                        return hit
            same_pkg = [c for c in cands if c.package == ctx.package]
            if same_pkg:
                return same_pkg[0]
            wild = [c for c in cands if any(i.endswith(".*") and i[:-2] == c.package for i in ctx.imports)]
            if wild:
                return wild[0]
            same_mod = [c for c in cands if c.module == ctx.module]
            if same_mod:
                return same_mod[0]
            if ctx.outer:
                nested = self.classes.get(f"{ctx.outer}.{simple}")
                if nested:
                    return nested
        return cands[0] if len(cands) == 1 else None

    def supertypes(self, c: ClassInfo) -> list[ClassInfo]:
        out: list[ClassInfo] = []
        for t in ([c.superclass] if c.superclass else []) + c.interfaces:
            r = self.resolve(t.name, c)
            if r:
                out.append(r)
        return out
