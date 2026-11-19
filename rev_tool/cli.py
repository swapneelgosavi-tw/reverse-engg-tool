"""rev-tool command line: index a repository into LadybugDB, then generate API specs + sequence diagrams."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

def _db_path(repo: Path, db_dir: Path) -> Path:
    return db_dir.resolve() / f"{repo.name}-db"


def cmd_index(repo: Path, db_dir: Path) -> int:
    from .indexer import build_graph

    print("indexing:", repo)
    stats = build_graph(repo, _db_path(repo, db_dir))
    print(f"indexed {stats['java_classes']} Java classes, {stats['js_modules']} JS/TS modules: "
          f"{stats['calls']} calls, {stats['http_calls']} HTTP calls")
    return 0


def cmd_generate(repo: Path, db_dir: Path, out_dir: Path, min_conf: float, max_depth: int, no_ui: bool) -> int:
    from .emit import write_all
    from .endpoints import EndpointExtractor
    from .flow import Tracer
    from .graph import CodeGraph
    from .javamodel import JavaProject
    from .schema import SchemaBuilder
    from .topology import Topology
    from .ui import UiLinker

    db = _db_path(repo, db_dir)
    if not db.exists():
        print(f"graph not found at {db}; run `rev-tool index` first", file=sys.stderr)
        return 2
    project = JavaProject(repo)
    topo = Topology(repo, {c.module for c in project.classes.values()})
    sb = SchemaBuilder(project)
    endpoints = EndpointExtractor(project, topo, sb).extract()
    if not endpoints:
        print("no Spring endpoints found", file=sys.stderr)
        return 1
    graph = CodeGraph(db)
    tracer = Tracer(project, graph, topo, endpoints, sb, min_confidence=min_conf, max_depth=max_depth)
    ui = None if no_ui else UiLinker(graph, topo, endpoints)
    summary = write_all(out_dir.resolve(), repo.name, endpoints, tracer, topo, ui)
    print(f"{summary['apis']} APIs -> {summary['output']}  (trace warnings: {summary['warnings']})")
    if sb.unresolved:
        print(f"unresolved types: {', '.join(sorted(sb.unresolved)[:15])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rev-tool", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("index", "parse the repo into a LadybugDB graph"),
                           ("generate", "emit per-API JSON and Mermaid diagrams from the graph"),
                           ("all", "index, then generate")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("repo", type=Path)
        sp.add_argument("--db-dir", type=Path, default=Path(".graph"))
        if name != "index":
            sp.add_argument("--out", type=Path, default=Path("output"))
            sp.add_argument("--min-confidence", type=float, default=0.9)
            sp.add_argument("--max-depth", type=int, default=14)
            sp.add_argument("--no-ui", action="store_true", help="skip React/TS caller linking")
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"not a directory: {repo}", file=sys.stderr)
        return 2
    if args.cmd in ("index", "all"):
        rc = cmd_index(repo, args.db_dir)
        if rc or args.cmd == "index":
            return rc
    return cmd_generate(repo, args.db_dir, args.out, args.min_confidence, args.max_depth, args.no_ui)


if __name__ == "__main__":
    raise SystemExit(main())
