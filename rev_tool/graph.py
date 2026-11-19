"""Read-only access to the LadybugDB graph produced by the reverselens indexer."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CallEdge:
    caller: str
    target: str
    line: int
    confidence: float
    reason: str


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


@dataclass
class NodeRef:
    id: str
    label: str
    name: str
    path: str | None = None
    language: str | None = None


@dataclass
class MethodId:
    cls_fqn: str
    name: str
    params: str  # param types joined with ',' and without spaces; '?' when the indexer could not resolve

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


_METHOD_ID = re.compile(r"^(?:method|constructor):(?P<head>[^()]+)\((?P<params>.*)\)$")


def parse_method_id(node_id: str) -> MethodId | None:
    m = _METHOD_ID.match(node_id)
    if not m:
        return None
    head = m.group("head")
    cls, _, name = head.rpartition(".")
    if not cls:
        return None
    return MethodId(cls.replace("$", "."), name, m.group("params").replace(" ", ""))


class CodeGraph:
    def __init__(self, db_path: str | Path):
        import ladybug

        self.db_path = str(Path(db_path).resolve())
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"LadybugDB database not found at {self.db_path}")
        self._db = ladybug.Database(self.db_path, read_only=True)
        self._conn = ladybug.Connection(self._db)
        self.calls_from: dict[str, list[CallEdge]] = defaultdict(list)
        self.callers_of: dict[str, list[CallEdge]] = defaultdict(list)
        self.ext_from: dict[str, list[ExtCall]] = defaultdict(list)
        self.nodes: dict[str, NodeRef] = {}
        self.renders: dict[str, list[str]] = defaultdict(list)  # parent component -> child components
        self._load()

    def query(self, cypher: str) -> list[dict[str, Any]]:
        res = self._conn.execute(cypher)
        cols = res.get_column_names()
        rows = []
        while res.has_next():
            rows.append(dict(zip(cols, res.get_next())))
        return rows

    def _load(self) -> None:
        for r in self.query(
            "MATCH (a)-[c:CALLS]->(b) RETURN a.id AS a, label(a) AS la, a.name AS an, a.path AS ap, a.language AS al,"
            " b.id AS b, label(b) AS lb, b.name AS bn, b.path AS bp, b.language AS bl,"
            " c.lineNumber AS ln, c.confidence AS cf, c.reason AS rs"
        ):
            self.nodes.setdefault(r["a"], NodeRef(r["a"], r["la"], r["an"], r["ap"], r["al"]))
            self.nodes.setdefault(r["b"], NodeRef(r["b"], r["lb"], r["bn"], r["bp"], r["bl"]))
            e = CallEdge(r["a"], r["b"], r["ln"] or 0, r["cf"] if r["cf"] is not None else 1.0, r["rs"] or "")
            self.calls_from[r["a"]].append(e)
            self.callers_of[r["b"]].append(e)
        for r in self.query(
            "MATCH (a)-[c:CALLS_EXTERNAL]->(e:ExternalApi) RETURN a.id AS a, label(a) AS la, a.name AS an,"
            " a.path AS ap, a.language AS al, e.httpMethod AS m, e.url AS url, e.host AS host, c.client AS cl,"
            " c.lineNumber AS ln, c.confidence AS cf, c.requestType AS rq, c.responseType AS rs, c.params AS pr,"
            " e.language AS el"
        ):
            self.nodes.setdefault(r["a"], NodeRef(r["a"], r["la"], r["an"], r["ap"], r["al"]))
            self.ext_from[r["a"]].append(ExtCall(
                r["a"], r["la"], r["an"], (r["m"] or "UNKNOWN").upper(), r["url"] or "", r["host"] or "",
                r["cl"] or "", r["ln"] or 0, r["cf"] if r["cf"] is not None else 1.0, r["rq"], r["rs"], r["pr"],
                r["el"] or r["al"]))
        self.ui_nodes: dict[str, list[NodeRef]] = defaultdict(list)  # file path -> components/hooks declared there
        for label in ("Component", "Hook"):
            for r in self.query(f"MATCH (c:{label}) RETURN c.id AS id, c.name AS n, c.path AS p, c.language AS l"):
                ref = self.nodes.setdefault(r["id"], NodeRef(r["id"], label, r["n"], r["p"], r["l"]))
                self.ui_nodes[r["p"] or ""].append(ref)
        try:
            for r in self.query("MATCH (a)-[:RENDERS]->(b) RETURN a.id AS a, b.id AS b"):
                self.renders[r["a"]].append(r["b"])
        except RuntimeError:
            pass
        for lst in self.calls_from.values():
            lst.sort(key=lambda e: e.line)
        for lst in self.ext_from.values():
            lst.sort(key=lambda e: e.line)

    def close(self) -> None:
        self._conn = None
        self._db = None
