"""Render a traced Diagram as Mermaid sequenceDiagram text."""

from __future__ import annotations

from typing import Any

from .flow import Block, Call, Diagram, Note

_MAX = 140
_ENTITIES = str.maketrans({"#": "#35;", ";": "#59;", "<": "#lt;", ">": "#gt;"})


def esc(text: str) -> str:
    text = " ".join(str(text).split())
    text = text.translate(_ENTITIES)
    return text if len(text) <= _MAX else text[: _MAX - 1] + "…"


def render(d: Diagram) -> str:
    lines = ["sequenceDiagram", f"    title {esc(d.title)}", "    autonumber"]
    groups: dict[str | None, list] = {}
    for p in d.participants.values():
        groups.setdefault(p.group if p.kind not in ("actor", "ui", "gateway") else None, []).append(p)
    boxed = len({g for g in groups if g}) > 1

    def declare(p, indent: str) -> str:
        keyword = "actor" if p.kind in ("actor", "ui") else "participant"
        return f"{indent}{keyword} {p.alias} as {esc(p.label)}"

    for p in groups.pop(None, []):
        lines.append(declare(p, "    "))
    for group, members in groups.items():
        if boxed:
            lines.append(f"    box {esc(group or '')}")
            lines += [declare(p, "        ") for p in members]
            lines.append("    end")
        else:
            lines += [declare(p, "    ") for p in members]

    _emit(d.items, lines, 1)
    return "\n".join(lines) + "\n"


def _emit(items: list[Any], out: list[str], level: int) -> None:
    pad = "    " * level
    for it in items:
        if isinstance(it, Note):
            out.append(f"{pad}Note over {it.over}: {esc(it.text)}")
        elif isinstance(it, Block):
            for i, (label, sub) in enumerate(it.branches):
                head = ("alt" if i == 0 else "else") if it.kind == "alt" else it.kind
                out.append(f"{pad}{head} {esc(label)}")
                _emit(sub, out, level + 1)
            out.append(f"{pad}end")
        elif isinstance(it, Call):
            _emit_call(it, out, level)


def _emit_call(c: Call, out: list[str], level: int) -> None:
    pad = "    " * level
    text = esc(c.text)
    if c.is_async:
        out.append(f"{pad}{c.src}-){c.dst}: {text}")
        if c.children:
            out.append(f"{pad}activate {c.dst}")
            _emit(c.children, out, level + 1)
            out.append(f"{pad}deactivate {c.dst}")
        return
    if c.ret is None:
        out.append(f"{pad}{c.src}->>{c.dst}: {text}")
        _emit(c.children, out, level + 1)
        return
    out.append(f"{pad}{c.src}->>+{c.dst}: {text}")
    _emit(c.children, out, level + 1)
    out.append(f"{pad}{c.dst}-->>-{c.src}: {esc(c.ret)}")
