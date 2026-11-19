# rev-tool

LLM-free reverse engineering of Java/Spring + React/TypeScript codebases. For every REST API it produces:

1. **Sequence diagram** (Mermaid) of the full flow: UI → gateway → controller → service → repository → DB, Feign/REST calls into other services, Kafka/Rabbit produce → consumer, Redis/cache.
2. **Request/response JSON** (OpenAPI-style operation) with fully expanded DTO schemas, validation constraints, examples, status codes and `@ControllerAdvice` error responses.

Everything is deterministic static analysis. The reverselens indexer (JavaParser + Babel → LadybugDB) supplies resolved call edges;
tree-sitter reads annotations, DTOs, messaging and data-store usage from source.

## Prerequisite: reverselens indexer

This tool reuses the [reverselens](../reverselens) indexer (expected at `../reverselens/indexer`, or set `INDEXER=<dir>`) and
does not bundle it. Linking React/TS calls written as root-relative URLs (`axios.get('/api/x')`) additionally needs
`parsers/javascript/outbound.js` to honour `REV_INCLUDE_RELATIVE=1`; without that change those calls are simply not linked.

## Usage

```bash
make build                         # once: venv if python3 exists, else falls back to pip3 (+ indexer jar if missing)
make build-pip                     # pip3 only, no venv/python command (pip3 must belong to Python 3.11+)
make all REPO=/path/to/repo        # index + generate
make index REPO=/path/to/repo      # only (re)build the graph  -> .graph/<repo>-db
make generate REPO=/path/to/repo   # only regenerate outputs   -> output/<repo>/
```

Options (call `.venv/bin/rev-tool generate --help`): `--out`, `--db-dir`, `--min-confidence` (default 0.9), `--max-depth`, `--no-ui`.
`INDEXER=<dir>` overrides the reverselens indexer location (default `../reverselens/indexer`).

## Output

```
output/<repo>/
  index.md
  apis/<service>/<METHOD>_<path>.json      entryPoint (first call + full URL), request params/body, responses, errorResponses, gateway, calledBy (UI)
  sequence/<service>/<METHOD>_<path>.mmd   Mermaid sequenceDiagram
```

Secrets from `application.properties`/`yml` are never written to the output.

## Notes and limits

- Requires Java 21+ and Node (for the indexer), and Python 3.11+ (`make build` uses `python3` and falls back to `pip3` when `python3` is missing; pass `PYTHON=/path/to/python3` if the default is older). With `build-pip`, `rev-tool` must be on PATH, or pass `REV=/full/path/to/rev-tool`.
- React/TS calls are linked by URL + HTTP method. Root-relative calls (`axios.get('/api/x')`) are ingested by `rev_tool/jsext.py`
  because the packaged indexer drops them.
- Branch labels (`alt`/`opt`) come from the source conditions; reflection, dynamic URLs and method references are not resolved.
- Calls below `--min-confidence` are skipped; same-class helper calls that make no further calls are hidden.
- Topic/queue names are resolved from literals, constants and properties only. JAX-RS is not supported.
- TypeScript DTO shapes are not expanded (the UI request/response type *names* appear in `calledBy`).
