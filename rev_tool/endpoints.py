"""Spring MVC endpoint extraction: paths, parameters, request body and response structures."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .javamodel import Annotation, ClassInfo, JavaProject, MethodInfo, ParamInfo, TypeRef
from .schema import SchemaBuilder, example_for, unwrap_type
from .topology import Topology

MAPPING_METHODS = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH", "RequestMapping": None,
}
CONTROLLER_ANNS = ("RestController", "Controller")

HTTP_STATUS = {
    "CONTINUE": 100, "OK": 200, "CREATED": 201, "ACCEPTED": 202, "NON_AUTHORITATIVE_INFORMATION": 203,
    "NO_CONTENT": 204, "RESET_CONTENT": 205, "PARTIAL_CONTENT": 206, "MOVED_PERMANENTLY": 301, "FOUND": 302,
    "SEE_OTHER": 303, "NOT_MODIFIED": 304, "TEMPORARY_REDIRECT": 307, "PERMANENT_REDIRECT": 308,
    "BAD_REQUEST": 400, "UNAUTHORIZED": 401, "PAYMENT_REQUIRED": 402, "FORBIDDEN": 403, "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405, "NOT_ACCEPTABLE": 406, "REQUEST_TIMEOUT": 408, "CONFLICT": 409, "GONE": 410,
    "PRECONDITION_FAILED": 412, "PAYLOAD_TOO_LARGE": 413, "UNSUPPORTED_MEDIA_TYPE": 415,
    "UNPROCESSABLE_ENTITY": 422, "LOCKED": 423, "TOO_MANY_REQUESTS": 429, "INTERNAL_SERVER_ERROR": 500,
    "NOT_IMPLEMENTED": 501, "BAD_GATEWAY": 502, "SERVICE_UNAVAILABLE": 503, "GATEWAY_TIMEOUT": 504,
}
_SHORTCUTS = {"ok": 200, "created": 201, "accepted": 202, "noContent": 204, "notFound": 404, "badRequest": 400,
              "unprocessableEntity": 422, "internalServerError": 500}
_IGNORED_PARAM_TYPES = {
    "HttpServletRequest", "HttpServletResponse", "ServletRequest", "ServletResponse", "HttpSession", "Principal",
    "Authentication", "Model", "ModelMap", "BindingResult", "Errors", "WebRequest", "NativeWebRequest",
    "UriComponentsBuilder", "Locale", "TimeZone", "ZoneId", "HttpMethod", "ServerWebExchange", "ServerHttpRequest",
    "ServerHttpResponse", "RedirectAttributes", "SessionStatus", "HttpEntity", "OutputStream", "Writer",
}
_IGNORED_PARAM_ANNS = {"AuthenticationPrincipal", "RequestAttribute", "SessionAttribute", "SessionScope",
                       "CurrentSecurityContext", "RegisteredOAuth2AuthorizedClient"}


@dataclass
class Param:
    name: str
    location: str  # path | query | header | cookie | body | formData
    type: str
    required: bool
    schema: dict[str, Any]
    default: Any = None
    description: str = ""


@dataclass
class Endpoint:
    service: str
    module: str
    http_method: str
    path: str  # path as declared in the controller (without context path)
    full_path: str  # context path + path
    cls: ClassInfo
    method: MethodInfo
    consumes: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    body: Param | None = None
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    error_responses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.http_method} {self.full_path}"

    @property
    def operation(self) -> str:
        return f"{self.cls.name}.{self.method.name}"


# ------------------------------------------------------------------ path helpers


def join_paths(*parts: str) -> str:
    segs: list[str] = []
    for p in parts:
        segs += [s for s in (p or "").split("/") if s]
    return "/" + "/".join(segs)


def norm_path(p: str) -> str:
    """Normalise template styles ({id}, :id, ${id}) so paths from code, UI and gateway compare equal."""
    p = p.split("?", 1)[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}/]*\}", "{}", p)
    p = re.sub(r"/:[A-Za-z_][\w]*", "/{}", p)
    p = re.sub(r"/{2,}", "/", p)
    return p.rstrip("/") or "/"


def path_matches(template: str, concrete: str) -> bool:
    return norm_path(template) == norm_path(concrete)


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def _mapping_of(anns: list[Annotation]) -> tuple[list[str] | None, list[str], list[str], list[str]] | None:
    """Return (http methods or None, paths, consumes, produces) for the first mapping annotation."""
    for a in anns:
        if a.name in MAPPING_METHODS:
            fixed = MAPPING_METHODS[a.name]
            methods = [fixed] if fixed else [m.rsplit(".", 1)[-1].upper() for m in _as_list(a.get("method"))]
            paths = _as_list(a.get("value", "path")) or [""]
            return (methods or None), paths, _as_list(a.get("consumes")), _as_list(a.get("produces"))
    return None


def _norm_media(values: list[str]) -> list[str]:
    out = []
    for v in values:
        v = v.rsplit("MediaType.", 1)[-1]
        out.append({"APPLICATION_JSON_VALUE": "application/json", "APPLICATION_JSON": "application/json",
                    "APPLICATION_XML_VALUE": "application/xml", "TEXT_PLAIN_VALUE": "text/plain",
                    "MULTIPART_FORM_DATA_VALUE": "multipart/form-data",
                    "APPLICATION_FORM_URLENCODED_VALUE": "application/x-www-form-urlencoded"}.get(v, v))
    return out


# ------------------------------------------------------------------ extraction


class EndpointExtractor:
    def __init__(self, project: JavaProject, topo: Topology, sb: SchemaBuilder):
        self.project = project
        self.topo = topo
        self.sb = sb
        self._advice: dict[str, list[dict[str, Any]]] = {}

    def extract(self) -> list[Endpoint]:
        self._collect_advice()
        out: list[Endpoint] = []
        for cls in sorted(self.project.classes.values(), key=lambda c: c.fqn):
            if cls.kind != "class" or not cls.has_ann(*CONTROLLER_ANNS) or cls.has_ann("ControllerAdvice", "RestControllerAdvice"):
                continue
            out += self._class_endpoints(cls)
        return out

    # -- class level
    def _class_mapping(self, cls: ClassInfo) -> tuple[list[str], list[str], list[str]]:
        for c in [cls] + self._supers(cls):
            ann = c.ann("RequestMapping")
            if ann is not None:
                paths = _as_list(ann.get("value", "path")) or [""]
                return paths, _norm_media(_as_list(ann.get("consumes"))), _norm_media(_as_list(ann.get("produces")))
        return [""], [], []

    def _supers(self, cls: ClassInfo, seen: set[str] | None = None) -> list[ClassInfo]:
        seen = seen or {cls.fqn}
        out: list[ClassInfo] = []
        for s in self.project.supertypes(cls):
            if s.fqn not in seen:
                seen.add(s.fqn)
                out.append(s)
                out += self._supers(s, seen)
        return out

    def _method_annotations(self, cls: ClassInfo, m: MethodInfo) -> tuple[MethodInfo, list[Annotation]]:
        """Mapping annotations may live on an implemented interface; parameter annotations then do too."""
        if _mapping_of(m.annotations):
            return m, m.annotations
        for sup in self._supers(cls):
            for sm in sup.methods:
                if sm.name == m.name and len(sm.params) == len(m.params) and _mapping_of(sm.annotations):
                    return sm, sm.annotations
        return m, m.annotations

    def _class_endpoints(self, cls: ClassInfo) -> list[Endpoint]:
        svc = self.topo.service_for_module(cls.module)
        base_paths, base_consumes, base_produces = self._class_mapping(cls)
        body_default = cls.has_ann("RestController") or cls.has_ann("ResponseBody")
        eps: list[Endpoint] = []
        for m in cls.methods:
            src_method, anns = self._method_annotations(cls, m)
            mapping = _mapping_of(anns)
            if mapping is None:
                continue
            if not body_default and not (m.ann("ResponseBody") or src_method.ann("ResponseBody")):
                continue
            methods, paths, consumes, produces = mapping
            for bp in base_paths:
                for mp in paths:
                    for hm in (methods or ["ANY"]):
                        rel = join_paths(bp, mp)
                        ep = Endpoint(
                            service=svc.name, module=cls.module, http_method=hm, path=rel,
                            full_path=join_paths(svc.context_path, rel), cls=cls, method=m,
                            consumes=_norm_media(consumes) or base_consumes,
                            produces=_norm_media(produces) or base_produces,
                        )
                        self._fill_params(ep, src_method)
                        self._fill_responses(ep, src_method)
                        eps.append(ep)
        return eps

    # -- parameters
    def _fill_params(self, ep: Endpoint, src: MethodInfo) -> None:
        ctx = ep.cls
        for i, p in enumerate(ep.method.params):
            sp = src.params[i] if i < len(src.params) else p
            anns = sp.annotations or p.annotations
            holder = ParamInfo(p.name, p.type, anns)
            self._classify_param(ep, holder, ctx, src)

    def _classify_param(self, ep: Endpoint, p: ParamInfo, ctx: ClassInfo, src: MethodInfo) -> None:
        simple = p.type.simple
        if simple in _IGNORED_PARAM_TYPES or any(a.name in _IGNORED_PARAM_ANNS for a in p.annotations):
            return
        doc = src.param_docs.get(p.name, "") or ep.method.param_docs.get(p.name, "")
        required_default = not (p.type.simple == "Optional")
        t = p.type.args[0] if p.type.simple == "Optional" and p.type.args else p.type

        if p.ann("RequestBody"):
            ann = p.ann("RequestBody")
            sch = self.sb.schema(t, ctx)
            ep.body = Param(p.name, "body", str(p.type), ann.get("required", default=True) is not False,
                            sch, description=doc)
            return
        if p.ann("PathVariable"):
            a = p.ann("PathVariable")
            name = a.get("value", "name") or p.name
            ep.params.append(Param(str(name), "path", str(p.type), True, self.sb.schema(t, ctx), description=doc))
            return
        if p.ann("RequestHeader"):
            a = p.ann("RequestHeader")
            name = a.get("value", "name") or p.name
            dv = a.get("defaultValue")
            req = a.get("required", default=True) is not False and dv is None and required_default
            ep.params.append(Param(str(name), "header", str(p.type), req, self.sb.schema(t, ctx), dv, doc))
            return
        if p.ann("CookieValue"):
            a = p.ann("CookieValue")
            ep.params.append(Param(str(a.get("value", "name") or p.name), "cookie", str(p.type),
                                   a.get("required", default=True) is not False, self.sb.schema(t, ctx),
                                   a.get("defaultValue"), doc))
            return
        if p.ann("RequestPart"):
            a = p.ann("RequestPart")
            ep.params.append(Param(str(a.get("value", "name") or p.name), "formData", str(p.type),
                                   a.get("required", default=True) is not False, self.sb.schema(t, ctx),
                                   description=doc))
            return
        if simple == "MultipartFile" or (p.type.args and p.type.args[0].simple == "MultipartFile"):
            ep.params.append(Param(p.name, "formData", str(p.type), True, self.sb.schema(p.type, ctx), description=doc))
            return
        if simple == "Pageable":
            for n, ty in (("page", "integer"), ("size", "integer"), ("sort", "string")):
                ep.params.append(Param(n, "query", "Pageable", False, {"type": ty}, description="Spring Data paging"))
            return
        a = p.ann("RequestParam")
        if a is not None or simple in ("String", "int", "Integer", "long", "Long", "boolean", "Boolean", "UUID",
                                       "List", "Set", "BigDecimal", "double", "Double", "LocalDate", "LocalDateTime"):
            name = (a.get("value", "name") if a else None) or p.name
            dv = a.get("defaultValue") if a else None
            req = (a.get("required", default=True) is not False) if a else True
            req = req and dv is None and required_default
            ep.params.append(Param(str(name), "query", str(p.type), req, self.sb.schema(t, ctx), dv, doc))
            return
        # unannotated POJO / @ModelAttribute -> its properties are bound from query string or form fields
        sch = self.sb.schema(t, ctx)
        if sch.get("type") == "object" and "properties" in sch:
            loc = "formData" if "application/x-www-form-urlencoded" in ep.consumes else "query"
            req = set(sch.get("required", []))
            for n, ps in sch["properties"].items():
                ep.params.append(Param(n, loc, str(ps.get("x-java-type", ps.get("type", ""))), n in req, ps,
                                       description=ps.get("description", "")))

    # -- responses
    def _fill_responses(self, ep: Endpoint, src: MethodInfo) -> None:
        m = ep.method
        payload, is_array = unwrap_type(m.return_type)
        schema = None
        if payload is not None:
            schema = self.sb.schema(payload, ep.cls)
            if is_array:
                schema = {"type": "array", "items": schema}
        rs = m.ann("ResponseStatus") or src.ann("ResponseStatus")
        declared = _status_code(rs.get("value", "code")) if rs else None
        found = _detect_statuses(m.body_text())

        def add(code: int, has_body: bool, note: str = "") -> None:
            entry: dict[str, Any] = {"description": _status_text(code)}
            if has_body and schema is not None:
                entry["type"] = str(payload) if not is_array else f"List<{payload}>"
                entry["contentType"] = (ep.produces or ["application/json"])[0] if not _is_text(schema) \
                    else (ep.produces or ["text/plain"])[0]
                entry["schema"] = schema
                entry["example"] = example_for(schema)
            if note:
                entry["note"] = note
            ep.responses.setdefault(str(code), entry)

        if declared is not None:
            add(declared, True)
        for code, has_body in found:
            add(code, has_body)
        if not ep.responses:
            add(200, schema is not None)
        ep.error_responses = list(self._advice.get(ep.module, []))

    # -- @ControllerAdvice
    def _collect_advice(self) -> None:
        for cls in self.project.classes.values():
            if not cls.has_ann("ControllerAdvice", "RestControllerAdvice"):
                continue
            for m in cls.methods:
                eh = m.ann("ExceptionHandler")
                if eh is None:
                    continue
                excs = [e.replace(".class", "") for e in _as_list(eh.get("value"))]
                if not excs:
                    excs = [p.type.simple for p in m.params if p.type.simple.endswith("Exception")]
                payload, is_array = unwrap_type(m.return_type)
                schema = self.sb.schema(payload, cls) if payload is not None else None
                if schema is not None and is_array:
                    schema = {"type": "array", "items": schema}
                rs = m.ann("ResponseStatus")
                status = _status_code(rs.get("value", "code")) if rs else None
                if status is None:
                    found = _detect_statuses(m.body_text())
                    status = found[0][0] if found else 200
                entry: dict[str, Any] = {"status": str(status), "exception": excs, "handler": f"{cls.name}.{m.name}",
                                         "description": _status_text(status)}
                if schema is not None:
                    entry["schema"] = schema
                    entry["example"] = example_for(schema)
                self._advice.setdefault(cls.module, []).append(entry)


def _is_text(schema: dict[str, Any]) -> bool:
    return schema.get("type") in ("string", "integer", "number", "boolean") and schema.get("format") not in ("binary",)


def _status_code(v: Any) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        name = v.rsplit(".", 1)[-1]
        if name in HTTP_STATUS:
            return HTTP_STATUS[name]
        if name.isdigit():
            return int(name)
    return None


def _status_text(code: int) -> str:
    for k, v in HTTP_STATUS.items():
        if v == code:
            return k.replace("_", " ").title()
    return str(code)


_RE_STATUS = re.compile(r"ResponseEntity\s*\.\s*status\s*\(\s*([\w.]+)\s*\)\s*((?:\.\s*\w+\s*\()?)")
_RE_SHORT = re.compile(r"ResponseEntity\s*\.\s*(ok|created|accepted|noContent|notFound|badRequest|"
                       r"unprocessableEntity|internalServerError)\s*\(([^)]*)\)\s*((?:\.\s*\w+\s*\()?)")
_RE_NEW = re.compile(r"new\s+ResponseEntity\s*(?:<[^>]*>)?\s*\(([^;]*?)\bHttpStatus\s*\.\s*(\w+)")
_RE_EXC = re.compile(r"new\s+ResponseStatusException\s*\(\s*(?:HttpStatus\s*\.\s*)?(\w+)")


def _detect_statuses(body: str) -> list[tuple[int, bool]]:
    found: list[tuple[int, bool]] = []

    def push(code: int | None, has_body: bool) -> None:
        if code is not None and all(c != code for c, _ in found):
            found.append((code, has_body))

    for m in _RE_STATUS.finditer(body):
        push(_status_code(m.group(1)), "build" not in m.group(2))
    for m in _RE_SHORT.finditer(body):
        name = m.group(1)
        has_body = "body" in m.group(3) or (name == "ok" and bool(m.group(2).strip()))
        push(_SHORTCUTS[name], has_body)
    for m in _RE_NEW.finditer(body):
        push(_status_code(m.group(2)), bool(m.group(1).strip().rstrip(",").strip()))
    for m in _RE_EXC.finditer(body):
        push(_status_code(m.group(1)), False)
    found.sort(key=lambda x: x[0])
    return found
