"""Code graph: an in-memory builder, a LadybugDB writer and a read-only loader.

Schema (LadybugDB)
  Sym(id, kind, name, path, language)         methods, functions, components, hooks
  ExtApi(id, httpMethod, url, host, client, language)
  CALLS(Sym -> Sym, line, seq, confidence, reason)
  CALLS_EXTERNAL(Sym -> ExtApi, line, seq, client, confidence, requestType, responseType, params)
"""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class CallEdge:
    caller: str
    target: str
    line: int
    confidence: float
    reason: str
    seq: int = 0


@dataclass
class ExtCall:
    caller: str
    caller_label: str
    caller_name: str
    method: str
    url: str
    host: str
    client: str
    line: int
    confidence: float
    request_type: str | None
    response_type: str | None
    params: str | None
    language: str | None
    seq: int = 0


@dataclass
class NodeRef:
    id: str
    label: str  # Method | Function | Component | Hook | Class
    name: str
    path: str | None = None
    language: str | None = None


@dataclass
class MethodId:
    cls_fqn: str
    name: str
    params: str  # param types joined with ',' and without spaces; '?' when unknown

    @property
    def arity(self) -> int:
        return 0 if not self.params else _split_top(self.params)


def _split_top(params: str) -> int:
    depth, n = 0, 1
    for ch in params:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
    return n


_METHOD_ID = re.compile(r"^method:(?P<head>[^()]+)\((?P<params>.*)\)$")


def method_id(cls_fqn: str, name: str, params: str) -> str:
    return f"method:{cls_fqn}.{name}({params.replace(' ', '')})"


def parse_method_id(node_id: str) -> MethodId | None:
    m = _METHOD_ID.match(node_id)
    if not m:
        return None
    cls, _, name = m.group("head").rpartition(".")
    if not cls:
        return None
    return MethodId(cls, name, m.group("params").replace(" ", ""))


def host_of(url: str) -> str:
    if "://" in url:
        return urlparse(url).netloc.rsplit("@", 1)[-1] or "(unknown)"
    return "(relative)" if url.startswith("/") else "(unknown)"


class GraphData:
    """Collects nodes and edges while indexing."""

    def __init__(self) -> None:
        self.nodes: dict[str, NodeRef] = {}
        self.calls: list[CallEdge] = []
        self.ext: list[ExtCall] = []
        self._seq = 0

    def add_node(self, node: NodeRef) -> None:
        self.nodes.setdefault(node.id, node)

    def add_call(self, caller: str, target: str, line: int, confidence: float, reason: str) -> None:
        self._seq += 1
        self.calls.append(CallEdge(caller, target, line, confidence, reason, self._seq))

    def add_ext(self, caller: str, method: str, url: str, client: str, line: int, confidence: float = 0.9,
                request_type: str | None = None, response_type: str | None = None, params: str | None = None,
                language: str = "java") -> None:
        node = self.nodes.get(caller)
        self._seq += 1
        self.ext.append(ExtCall(caller, node.label if node else "Method", node.name if node else caller,
                                method.upper(), url, host_of(url), client, line, confidence, request_type,
                                response_type, params, language, self._seq))

    def write(self, db_path: str | Path) -> None:
        import ladybug

        path = Path(db_path)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        db = ladybug.Database(str(path))
        conn = ladybug.Connection(db)
        for ddl in (
            "CREATE NODE TABLE Sym(id STRING PRIMARY KEY, kind STRING, name STRING, path STRING, language STRING)",
            "CREATE NODE TABLE ExtApi(id STRING PRIMARY KEY, httpMethod STRING, url STRING, host STRING,"
            " client STRING, language STRING)",
            "CREATE REL TABLE CALLS(FROM Sym TO Sym, line INT64, seq INT64, confidence DOUBLE, reason STRING)",
            "CREATE REL TABLE CALLS_EXTERNAL(FROM Sym TO ExtApi, line INT64, seq INT64, client STRING,"
            " confidence DOUBLE, requestType STRING, responseType STRING, params STRING)",
        ):
            conn.execute(ddl)

        def bulk(query: str, rows: list[dict], chunk: int = 5000) -> None:
            for i in range(0, len(rows), chunk):
                conn.execute(query, {"rows": rows[i:i + chunk]})

        bulk("UNWIND $rows AS r CREATE (:Sym {id: r.id, kind: r.kind, name: r.name, path: r.path,"
             " language: r.language})",
             [{"id": n.id, "kind": n.label, "name": n.name, "path": n.path or "", "language": n.language or ""}
              for n in self.nodes.values()])
        apis: dict[str, dict] = {}
        for x in self.ext:
            apis.setdefault(f"ext:{x.method} {x.url}", {"id": f"ext:{x.method} {x.url}", "m": x.method, "url": x.url,
                                                        "host": x.host, "client": x.client, "lang": x.language or ""})
        bulk("UNWIND $rows AS r CREATE (:ExtApi {id: r.id, httpMethod: r.m, url: r.url, host: r.host,"
             " client: r.client, language: r.lang})", list(apis.values()))
        bulk("UNWIND $rows AS r MATCH (a:Sym {id: r.s}), (b:Sym {id: r.t}) CREATE (a)-[:CALLS {line: r.line,"
             " seq: r.seq, confidence: r.cf, reason: r.rs}]->(b)",
             [{"s": e.caller, "t": e.target, "line": e.line, "seq": e.seq, "cf": e.confidence, "rs": e.reason}
              for e in self.calls if e.target in self.nodes and e.caller in self.nodes])
        bulk("UNWIND $rows AS r MATCH (a:Sym {id: r.s}), (b:ExtApi {id: r.t}) CREATE (a)-[:CALLS_EXTERNAL {line: r.line,"
             " seq: r.seq, client: r.client, confidence: r.cf, requestType: r.rq, responseType: r.rs,"
             " params: r.pr}]->(b)",
             [{"s": x.caller, "t": f"ext:{x.method} {x.url}", "line": x.line, "seq": x.seq, "client": x.client,
               "cf": x.confidence, "rq": x.request_type or "", "rs": x.response_type or "", "pr": x.params or ""}
              for x in self.ext if x.caller in self.nodes])


class CodeGraph:
    """Read-only view of a graph written by GraphData.write()."""

    def __init__(self, db_path: str | Path):
        import ladybug

        self.db_path = str(Path(db_path).resolve())
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"graph database not found at {self.db_path}")
        self._db = ladybug.Database(self.db_path, read_only=True)
        self._conn = ladybug.Connection(self._db)
        self.calls_from: dict[str, list[CallEdge]] = defaultdict(list)
        self.callers_of: dict[str, list[CallEdge]] = defaultdict(list)
        self.ext_from: dict[str, list[ExtCall]] = defaultdict(list)
        self.nodes: dict[str, NodeRef] = {}
        self.ui_nodes: dict[str, list[NodeRef]] = defaultdict(list)  # file path -> components/hooks declared there
        self._load()

    def query(self, cypher: str) -> list[dict]:
        res = self._conn.execute(cypher)
        cols = res.get_column_names()
        rows = []
        while res.has_next():
            rows.append(dict(zip(cols, res.get_next())))
        return rows

    def _load(self) -> None:
        for r in self.query("MATCH (n:Sym) RETURN n.id AS id, n.kind AS k, n.name AS n, n.path AS p, n.language AS l"):
            ref = NodeRef(r["id"], r["k"], r["n"], r["p"] or None, r["l"] or None)
            self.nodes[ref.id] = ref
            if ref.label in ("Component", "Hook"):
                self.ui_nodes[ref.path or ""].append(ref)
        for r in self.query("MATCH (a:Sym)-[c:CALLS]->(b:Sym) RETURN a.id AS a, b.id AS b, c.line AS ln,"
                            " c.seq AS sq, c.confidence AS cf, c.reason AS rs"):
            e = CallEdge(r["a"], r["b"], r["ln"] or 0, r["cf"] if r["cf"] is not None else 1.0, r["rs"] or "",
                         r["sq"] or 0)
            self.calls_from[e.caller].append(e)
            self.callers_of[e.target].append(e)
        for r in self.query("MATCH (a:Sym)-[c:CALLS_EXTERNAL]->(e:ExtApi) RETURN a.id AS a, e.httpMethod AS m,"
                            " e.url AS url, e.host AS host, e.language AS el, c.client AS cl, c.line AS ln,"
                            " c.seq AS sq, c.confidence AS cf, c.requestType AS rq, c.responseType AS rs,"
                            " c.params AS pr"):
            node = self.nodes.get(r["a"])
            self.ext_from[r["a"]].append(ExtCall(
                r["a"], node.label if node else "", node.name if node else r["a"], r["m"] or "GET", r["url"] or "",
                r["host"] or "", r["cl"] or "", r["ln"] or 0, r["cf"] if r["cf"] is not None else 0.9,
                r["rq"] or None, r["rs"] or None, r["pr"] or None, r["el"] or (node.language if node else None),
                r["sq"] or 0))
        for lst in (*self.calls_from.values(), *self.ext_from.values(), *self.callers_of.values()):
            lst.sort(key=lambda e: (e.line, e.seq))

    def close(self) -> None:
        self._conn = None
        self._db = None
