# rev-tool

LLM-free reverse engineering of Java/Spring + React/TypeScript codebases. For every REST API it produces:

1. **Sequence diagram** (Mermaid) of the full flow: UI → gateway → controller → service → repository → DB, Feign/REST calls into other services, Kafka/Rabbit produce → consumer, Redis/cache.
2. **Request/response JSON** (OpenAPI-style operation) with fully expanded DTO schemas, validation constraints, examples, status codes and `@ControllerAdvice` error responses.

Everything is deterministic static analysis and fully standalone: tree-sitter parses Java and TypeScript/JavaScript, resolves
call edges and outbound HTTP calls in pure Python, and stores the call graph in LadybugDB. Only Python 3.11+ and pip are required
(no Java, Node, Gradle or external indexer).

## Usage

```bash
make build                         # once: venv if python3 exists, else falls back to pip3
make build-pip                     # pip3 only, no venv/python command (pip3 must belong to Python 3.11+); re-run after editing rev_tool/
make all REPO=/path/to/repo        # index + generate
make index REPO=/path/to/repo      # only (re)build the graph  -> .graph/<repo>-db
make generate REPO=/path/to/repo   # only regenerate outputs   -> output/<repo>/
```

Options (call `.venv/bin/rev-tool generate --help`): `--out`, `--db-dir`, `--min-confidence` (default 0.9), `--max-depth`, `--no-ui`.

## Output

```
output/<repo>/
  index.md
  apis/<service>/<METHOD>_<path>.json      entryPoint (first call + full URL), request params/body, responses, errorResponses, gateway, calledBy (UI)
  sequence/<service>/<METHOD>_<path>.mmd   Mermaid sequenceDiagram
```

Secrets from `application.properties`/`yml` are never written to the output.

## Notes and limits

- Requires Python 3.11+ only (`make build` uses `python3` and falls back to `pip3` when `python3` is missing; pass `PYTHON=/path/to/python3` if the default is older). With `build-pip`, `rev-tool` must be on PATH, or pass `REV=/full/path/to/rev-tool`.
- Java calls are resolved from declared types of parameters, fields and locals (no compiler). Calls on JDK/library types are ignored; ambiguous overloads are matched by arity and argument types.
- Outbound HTTP is detected for Feign, RestTemplate, WebClient, RestClient (Java) and axios (including `axios.create` instances) and `fetch` (React/TS).
- React/TS calls are linked by URL + HTTP method, through relative imports, `@/` aliases, re-exports and object-literal API modules.
- Branch labels (`alt`/`opt`) come from the source conditions; reflection and fully dynamic URLs are not resolved.
- Calls below `--min-confidence` are skipped; same-class helper calls that make no further calls are hidden.
- Topic/queue names are resolved from literals, constants and properties only. JAX-RS is not supported.
- TypeScript DTO shapes are not expanded (the UI request/response type *names* appear in `calledBy`).
