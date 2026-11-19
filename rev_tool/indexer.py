"""Build the LadybugDB code graph for a repository using only Python (tree-sitter)."""

from __future__ import annotations

import sys
from pathlib import Path

from .graph import GraphData
from .javacalls import JavaCallIndexer
from .javamodel import JavaProject
from .jscalls import JsCallIndexer


def build_graph(repo: Path, db_path: Path, project: JavaProject | None = None) -> dict[str, int]:
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    graph = GraphData()
    project = project or JavaProject(repo)
    JavaCallIndexer(project, graph).run()
    js_modules = JsCallIndexer(repo, graph).run()
    graph.write(db_path)
    return {
        "java_classes": len(project.classes),
        "js_modules": js_modules,
        "nodes": len(graph.nodes),
        "calls": len(graph.calls),
        "http_calls": len(graph.ext),
    }
