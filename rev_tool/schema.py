"""Java type -> JSON-schema-like structure (full depth) plus example payload generation."""

from __future__ import annotations

from typing import Any

from .javamodel import Annotation, ClassInfo, FieldInfo, JavaProject, TypeRef

MAX_DEPTH = 14

_STRING = {"String", "CharSequence", "Character", "char", "StringBuilder"}
_INT32 = {"int", "Integer", "short", "Short", "byte", "Byte"}
_INT64 = {"long", "Long", "BigInteger", "AtomicLong"}
_NUMBER = {"float", "Float", "double", "Double", "Number"}
_DECIMAL = {"BigDecimal"}
_BOOL = {"boolean", "Boolean"}
_DATE = {"LocalDate", "java.sql.Date"}
_DATETIME = {"LocalDateTime", "OffsetDateTime", "ZonedDateTime", "Instant", "Date", "Timestamp", "Calendar"}
_TIME = {"LocalTime", "OffsetTime"}
_STRING_FMT = {
    "UUID": "uuid", "URI": "uri", "URL": "uri", "Currency": "currency", "Locale": "locale",
    "Duration": "duration", "Period": "duration", "ZoneId": "timezone", "YearMonth": "year-month",
}
_COLLECTIONS = {"List", "Set", "Collection", "Iterable", "ArrayList", "LinkedList", "HashSet", "LinkedHashSet",
                "TreeSet", "SortedSet", "Queue", "Deque", "Stream", "Flux"}
_MAPS = {"Map", "HashMap", "LinkedHashMap", "TreeMap", "SortedMap", "ConcurrentHashMap", "MultiValueMap"}
_WRAPPERS = {"Optional", "ResponseEntity", "HttpEntity", "Mono", "CompletableFuture", "Future", "Callable",
             "DeferredResult", "CompletionStage", "ListenableFuture", "AtomicReference", "WebAsyncTask"}
_BINARY = {"MultipartFile", "Resource", "InputStreamResource", "ByteArrayResource", "InputStream", "File",
           "StreamingResponseBody"}
_ANY = {"Object", "JsonNode", "ObjectNode", "JSONObject", "Any"}
SIMPLE_JSON = _STRING | _INT32 | _INT64 | _NUMBER | _DECIMAL | _BOOL | _DATE | _DATETIME | _TIME | set(_STRING_FMT)

def unwrap_type(t: TypeRef) -> tuple[TypeRef | None, bool]:
    """Strip response/async wrappers. Returns (payload type or None for void, is_array_from_flux)."""
    is_array = False
    while True:
        s = t.simple
        if s == "void" or s == "Void":
            return None, is_array
        if s in _WRAPPERS and t.args:
            t = t.args[0]
        elif s in _WRAPPERS and not t.args:
            return TypeRef("Object"), is_array
        elif s == "Flux" and t.args:
            is_array = True
            t = t.args[0]
        else:
            return t, is_array


class SchemaBuilder:
    def __init__(self, project: JavaProject):
        self.project = project
        self.unresolved: set[str] = set()

    # ------------------------------------------------------------------ public
    def schema(self, t: TypeRef, ctx: ClassInfo | None, bindings: dict[str, TypeRef] | None = None,
               _stack: tuple[str, ...] = ()) -> dict[str, Any]:
        bindings = bindings or {}
        if t.dims:
            if t.dims == 1 and t.name == "byte":
                return {"type": "string", "format": "byte"}
            inner = TypeRef(t.name, t.args, t.dims - 1)
            return {"type": "array", "items": self.schema(inner, ctx, bindings, _stack)}

        name, simple = t.name, t.simple
        if name in bindings:
            return self.schema(bindings[name], ctx, {}, _stack)
        if simple in _STRING:
            return {"type": "string"}
        if simple in _INT32:
            return {"type": "integer", "format": "int32"}
        if simple in _INT64:
            return {"type": "integer", "format": "int64"}
        if simple in _NUMBER:
            return {"type": "number", "format": "double" if simple in ("double", "Double") else None}
        if simple in _DECIMAL:
            return {"type": "number", "format": "decimal"}
        if simple in _BOOL:
            return {"type": "boolean"}
        if simple in _DATE:
            return {"type": "string", "format": "date"}
        if simple in _DATETIME:
            return {"type": "string", "format": "date-time"}
        if simple in _TIME:
            return {"type": "string", "format": "time"}
        if simple in _STRING_FMT and not self.project.resolve(name, ctx):
            return {"type": "string", "format": _STRING_FMT[simple]}
        if simple in _BINARY and not self.project.resolve(name, ctx):
            return {"type": "string", "format": "binary"}
        if simple in _ANY and not self.project.resolve(name, ctx):
            return {"type": "object", "additionalProperties": True}
        if simple in _WRAPPERS and t.args and not self.project.resolve(name, ctx):
            return self.schema(t.args[0], ctx, bindings, _stack)
        if simple in _COLLECTIONS and not self.project.resolve(name, ctx):
            item = t.args[0] if t.args else TypeRef("Object")
            return {"type": "array", "items": self.schema(item, ctx, bindings, _stack)}
        if simple in _MAPS and not self.project.resolve(name, ctx):
            val = t.args[1] if len(t.args) > 1 else TypeRef("Object")
            return {"type": "object", "additionalProperties": self.schema(val, ctx, bindings, _stack)}
        if simple == "Page" and not self.project.resolve(name, ctx):
            item = t.args[0] if t.args else TypeRef("Object")
            return {"type": "object", "title": "Page", "properties": {
                "content": {"type": "array", "items": self.schema(item, ctx, bindings, _stack)},
                "totalElements": {"type": "integer", "format": "int64"},
                "totalPages": {"type": "integer", "format": "int32"},
                "number": {"type": "integer", "format": "int32"},
                "size": {"type": "integer", "format": "int32"}}}

        cls = self.project.resolve(name, ctx)
        if cls is None:
            self.unresolved.add(name)
            return {"type": "object", "x-java-type": str(t), "x-unresolved": True}
        return self._class_schema(cls, t, bindings, _stack)

    def class_schema(self, cls: ClassInfo) -> dict[str, Any]:
        return self._class_schema(cls, TypeRef(cls.name), {}, ())

    # ------------------------------------------------------------------ classes
    def _class_schema(self, cls: ClassInfo, ref: TypeRef, outer_bindings: dict[str, TypeRef],
                      stack: tuple[str, ...]) -> dict[str, Any]:
        if cls.kind == "enum":
            return {"type": "string", "title": cls.name, "enum": list(cls.enum_constants), "x-java-type": cls.fqn}
        if cls.fqn in stack or len(stack) > MAX_DEPTH:
            return {"type": "object", "title": cls.name, "x-recursive-ref": cls.name}

        args = [self._resolve_binding(a, outer_bindings) for a in ref.args]
        bindings = dict(zip(cls.type_params, args))
        stack = stack + (cls.fqn,)

        props: dict[str, Any] = {}
        required: list[str] = []
        self._collect_fields(cls, bindings, stack, props, required, set())
        schema: dict[str, Any] = {"type": "object", "title": cls.name, "x-java-type": cls.fqn}
        if cls.doc:
            schema["description"] = cls.doc
        schema["properties"] = props
        if required:
            schema["required"] = required
        return schema

    @staticmethod
    def _resolve_binding(t: TypeRef, bindings: dict[str, TypeRef]) -> TypeRef:
        if t.name in bindings and not t.args:
            b = bindings[t.name]
            return TypeRef(b.name, b.args, b.dims + t.dims)
        return TypeRef(t.name, [SchemaBuilder._resolve_binding(a, bindings) for a in t.args], t.dims)

    def _collect_fields(self, cls: ClassInfo, bindings: dict[str, TypeRef], stack: tuple[str, ...],
                        props: dict[str, Any], required: list[str], seen: set[str]) -> None:
        if cls.superclass is not None:
            sup = self.project.resolve(cls.superclass.name, cls)
            if sup is not None and sup.fqn not in seen:
                seen.add(sup.fqn)
                sup_args = [self._resolve_binding(a, bindings) for a in cls.superclass.args]
                self._collect_fields(sup, dict(zip(sup.type_params, sup_args)), stack, props, required, seen)
        for f in cls.fields:
            if "static" in f.modifiers or "transient" in f.modifiers:
                continue
            if f.ann("JsonIgnore"):
                continue
            prop = self._json_name(f)
            fs = self.schema(f.type, cls, bindings, stack)
            self._apply_annotations(f, fs, f.type)
            props[prop] = fs
            if self._is_required(f):
                required.append(prop)

    @staticmethod
    def _json_name(f: FieldInfo) -> str:
        jp = f.ann("JsonProperty")
        if jp is not None:
            v = jp.get("value", "name")
            if isinstance(v, str) and v:
                return v
        return f.name

    @staticmethod
    def _is_required(f: FieldInfo) -> bool:
        if f.type.simple == "Optional":
            return False
        for a in f.annotations:
            if a.name in ("NotNull", "NonNull", "NotBlank", "NotEmpty"):
                return True
            if a.name == "JsonProperty" and a.get("required") is True:
                return True
            if a.name in ("Schema", "ApiModelProperty") and (a.get("required") is True
                                                             or a.get("requiredMode") == "RequiredMode.REQUIRED"):
                return True
        return False

    def _apply_annotations(self, f: FieldInfo, schema: dict[str, Any], t: TypeRef) -> None:
        is_array = schema.get("type") == "array"
        is_string = schema.get("type") == "string"
        for a in f.annotations:
            n = a.name
            if n in ("NotBlank", "NotEmpty"):
                schema["minItems" if is_array else "minLength"] = 1
            elif n in ("Size", "Length"):
                lo, hi = a.get("min"), a.get("max")
                if isinstance(lo, int) and lo:
                    schema["minItems" if is_array else "minLength"] = lo
                if isinstance(hi, int) and hi != 2147483647:
                    schema["maxItems" if is_array else "maxLength"] = hi
            elif n in ("Min", "DecimalMin"):
                v = _num(a.get("value"))
                if v is not None:
                    schema["minimum"] = v
            elif n in ("Max", "DecimalMax"):
                v = _num(a.get("value"))
                if v is not None:
                    schema["maximum"] = v
            elif n == "Range":
                lo, hi = _num(a.get("min")), _num(a.get("max"))
                if lo is not None:
                    schema["minimum"] = lo
                if hi is not None:
                    schema["maximum"] = hi
            elif n == "Positive":
                schema["exclusiveMinimum"] = 0
            elif n == "PositiveOrZero":
                schema["minimum"] = 0
            elif n == "Negative":
                schema["exclusiveMaximum"] = 0
            elif n == "NegativeOrZero":
                schema["maximum"] = 0
            elif n == "Pattern":
                rx = a.get("regexp", "value")
                if isinstance(rx, str):
                    schema["pattern"] = rx
            elif n == "Email":
                schema["format"] = "email"
            elif n == "Digits":
                schema["x-digits"] = {"integer": a.get("integer"), "fraction": a.get("fraction")}
            elif n in ("Past", "PastOrPresent", "Future", "FutureOrPresent"):
                schema["x-temporal"] = n
            elif n == "JsonFormat":
                pat = a.get("pattern")
                if isinstance(pat, str) and is_string:
                    schema["x-format-pattern"] = pat
                    schema.pop("format", None)
            elif n in ("Schema", "ApiModelProperty"):
                for key, out in (("description", "description"), ("value", "description"), ("example", "example"),
                                 ("defaultValue", "default")):
                    v = a.get(key)
                    if v not in (None, "") and out not in schema:
                        schema[out] = v
            elif n == "Column" and is_string and isinstance(a.get("length"), int):
                schema.setdefault("maxLength", a.get("length"))
        if f.doc and "description" not in schema:
            schema["description"] = f.doc
        if schema.get("format") is None:
            schema.pop("format", None)


def _num(v: Any) -> Any:
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------- examples

_FORMAT_EXAMPLES = {
    "date": "2024-01-15", "date-time": "2024-01-15T10:30:00Z", "time": "10:30:00", "uuid":
    "3fa85f64-5717-4562-b3fc-2c963f66afa6", "email": "user@example.com", "uri": "https://example.com",
    "currency": "USD", "locale": "en_US", "duration": "PT30M", "timezone": "Asia/Kolkata", "year-month": "2024-01",
    "binary": "<binary>", "byte": "<base64>",
}


def example_for(schema: dict[str, Any], depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return None
    if "example" in schema:
        return schema["example"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        if "properties" in schema:
            return {k: example_for(v, depth + 1) for k, v in schema["properties"].items()}
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            return {"key": example_for(ap, depth + 1)}
        return {}
    if t == "array":
        return [example_for(schema.get("items", {}), depth + 1)]
    if t == "string":
        return _FORMAT_EXAMPLES.get(schema.get("format"), "string")
    if t == "integer":
        return schema.get("minimum", 0) if isinstance(schema.get("minimum"), int) else 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return True
    return None
