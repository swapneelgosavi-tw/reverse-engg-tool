"""rev-tool command line: index a repository into LadybugDB, then generate API specs + sequence diagrams."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Defaults are relative to the working directory (run from the tool folder, as `make` does), so they also hold
# when the package is installed non-editably into site-packages.
DEFAULT_INDEXER = Path("..") / "reverselens" / "indexer"


def _indexer_dir(arg: str | None) -> Path:
    return Path(arg or os.environ.get("REV_INDEXER_DIR") or DEFAULT_INDEXER).resolve()


def _db_path(repo: Path, db_dir: Path) -> Path:
    return db_dir.resolve() / f"{repo.name}-db"


def cmd_index(repo: Path, db_dir: Path, indexer: Path) -> int:
    jar = indexer / "app" / "build" / "libs" / "reverselens-indexer.jar"
    if not jar.exists():
        print(f"indexer jar not found: {jar}\nbuild it first: cd {indexer} && ./install.sh", file=sys.stderr)
        return 2
    db_dir.mkdir(parents=True, exist_ok=True)
    # The indexer locates its JS parser relative to the working directory, so it must run from the indexer dir.
    cmd = ["java", "-jar", str(jar), str(repo), "--db-path", str(db_dir.resolve())]
    print("indexing:", repo)
    rc = subprocess.run(cmd, cwd=indexer).returncode
    if rc:
        return rc
    from .jsext import ingest

    try:
        n = ingest(repo, _db_path(repo, db_dir), indexer)
        if n:
            print(f"linked {n} React/TS outbound HTTP call(s)")
    except Exception as exc:  # the backend graph is still usable without UI links
        print(f"warning: React/TS outbound call ingestion failed: {exc}", file=sys.stderr)
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
        sp.add_argument("--indexer-dir", default=None, help="reverselens indexer directory")
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
        rc = cmd_index(repo, args.db_dir, _indexer_dir(args.indexer_dir))
        if rc or args.cmd == "index":
            return rc
    return cmd_generate(repo, args.db_dir, args.out, args.min_confidence, args.max_depth, args.no_ui)


if __name__ == "__main__":
    raise SystemExit(main())
