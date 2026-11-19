"""Append React/TypeScript outbound HTTP calls (ExternalApi nodes + CALLS_EXTERNAL edges) to the graph.

The packaged indexer's Java bridge drops these from the Babel parser output, so they are ingested here from the
parser's JSON backend. The parser must run on the same repo root so node ids match the ones already stored.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _run_parser(repo: Path, indexer: Path) -> dict:
    script = indexer / "parsers" / "javascript" / "parse.js"
    env = {**os.environ, "REV_INCLUDE_RELATIVE": "1"}  # keep UI calls like axios.get('/api/x')
    res = subprocess.run(["node", str(script), str(repo), "--backend", "json"], capture_output=True, text=True,
                         env=env, cwd=script.parent)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-500:])
    out = res.stdout
    return json.loads(out[out.find("{"):])


def ingest(repo: Path, db_path: Path, indexer: Path) -> int:
    import ladybug

    data = _run_parser(repo, indexer)
    nodes = data["nodes"].values() if isinstance(data["nodes"], dict) else data["nodes"]
    apis = [n for n in nodes if n["type"] == "EXTERNAL_API"]
    edges = [r for r in data["relationships"] if r["type"] == "CALLS_EXTERNAL"]
    if not edges:
        return 0
    db = ladybug.Database(str(db_path))
    conn = ladybug.Connection(db)

    def run(q: str, params: dict | None = None):
        return conn.execute(q, params or {})

    for n in apis:
        p = n.get("properties", {})
        run("MERGE (e:ExternalApi {id: $id}) SET e.name = $name, e.qualifiedName = $qn, e.httpMethod = $m,"
            " e.url = $url, e.host = $host, e.client = $client, e.language = $lang",
            {"id": n["id"], "name": n["name"], "qn": n.get("qualifiedName") or "", "m": p.get("httpMethod", "GET"),
             "url": p.get("url", ""), "host": p.get("host", ""), "client": p.get("client", ""),
             "lang": p.get("language", "")})
    added = 0
    for r in edges:
        res = run("MATCH (a) WHERE a.id = $id RETURN label(a) AS l", {"id": r["sourceId"]})
        if not res.has_next():
            continue
        label = res.get_next()[0]
        p = r.get("properties", {})
        run(f"MATCH (a:{label} {{id: $src}}), (e:ExternalApi {{id: $dst}}) CREATE (a)-[:CALLS_EXTERNAL "
            "{lineNumber: $ln, client: $client, confidence: $cf, reason: $reason, requestType: $rq,"
            " responseType: $rs, params: $pr}]->(e)",
            {"src": r["sourceId"], "dst": r["targetId"], "ln": int(p.get("lineNumber", 0)),
             "client": p.get("client", ""), "cf": float(p.get("confidence", 0.9)), "reason": p.get("reason", ""),
             "rq": p.get("requestType") or "", "rs": p.get("responseType") or "", "pr": p.get("params") or ""})
        added += 1
    return added
