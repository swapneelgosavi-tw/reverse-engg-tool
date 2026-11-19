PYTHON  ?= python3
PIP     ?= pip3
VENV    := .venv
# use the venv's rev-tool when it exists, otherwise the one installed by pip3 on PATH
REV     ?= $(if $(wildcard $(VENV)/bin/rev-tool),$(VENV)/bin/rev-tool,rev-tool)
OUT     ?= output
INDEXER ?= ../reverselens/indexer
JAR     := $(INDEXER)/app/build/libs/reverselens-indexer.jar

.PHONY: build build-venv build-pip check-indexer index generate all clean help

help:
	@echo "make build                  one-time setup: venv if $(PYTHON) exists, otherwise pip3 (no python needed)"
	@echo "make build-pip              install with pip3 only (no venv); pip3 must belong to Python 3.11+"
	@echo "make index    REPO=<path>   parse repo into LadybugDB graph"
	@echo "make generate REPO=<path>   write sequence diagrams + API request/response JSON to $(OUT)/"
	@echo "make all      REPO=<path>   index + generate"

build:
	@if command -v $(PYTHON) >/dev/null 2>&1; then $(MAKE) --no-print-directory build-venv; \
	else echo "$(PYTHON) not found; falling back to $(PIP)"; $(MAKE) --no-print-directory build-pip; fi

build-venv:
	@$(PYTHON) -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'python 3.11+ required; pass PYTHON=/path/to/python3 or use make build-pip')"
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet -e .
	@$(MAKE) --no-print-directory check-indexer
	@echo "ready: $(VENV)/bin/rev-tool"

# non-editable: old pip (<21.3) cannot do editable installs of pyproject-only projects; re-run after changing rev_tool/
build-pip:
	$(PIP) install --upgrade .
	@$(MAKE) --no-print-directory check-indexer
	@command -v rev-tool >/dev/null 2>&1 || echo "note: rev-tool is not on PATH; add the scripts directory printed by '$(PIP) show -f rev-tool' to PATH, or run make with REV=/full/path/to/rev-tool"
	@echo "ready: rev-tool"

check-indexer:
	@test -d "$(INDEXER)" || { echo "ERROR: reverselens indexer not found at $(INDEXER)."; \
	  echo "  Clone/copy reverselens next to this folder or pass INDEXER=/path/to/reverselens/indexer"; exit 1; }
	@test -f "$(JAR)" || { echo "indexer jar missing; building (needs Java 21+ and network on first run)..."; \
	  command -v java >/dev/null 2>&1 || { echo "ERROR: java not found on PATH (Java 21+ required)"; exit 1; }; \
	  (cd "$(INDEXER)" && ./gradlew shadowJar) || true; }
	@test -f "$(JAR)" || { echo "ERROR: indexer jar still missing at $(JAR)."; \
	  echo "  Build it manually: cd $(INDEXER) && ./gradlew shadowJar   (see the Gradle error above)"; \
	  echo "  Or copy a prebuilt reverselens-indexer.jar to that path."; exit 1; }

index generate all:
	@test "$@" = generate || $(MAKE) --no-print-directory check-indexer
	@test -n "$(REPO)" || (echo "usage: make $@ REPO=/path/to/repo" && exit 2)
	$(REV) $@ "$(REPO)" $(if $(filter-out index,$@),--out "$(OUT)") --indexer-dir "$(INDEXER)"

clean:
	rm -rf .graph $(OUT)
