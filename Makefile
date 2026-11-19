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

build-pip:
	$(PIP) install -e .
	@$(MAKE) --no-print-directory check-indexer
	@command -v rev-tool >/dev/null 2>&1 || echo "note: rev-tool is not on PATH; add the scripts directory printed by '$(PIP) show -f rev-tool' to PATH, or run make with REV=/full/path/to/rev-tool"
	@echo "ready: rev-tool"

check-indexer:
	@test -f $(JAR) || (echo "indexer jar missing; building..." && cd $(INDEXER) && ./install.sh)

index generate all:
	@test -n "$(REPO)" || (echo "usage: make $@ REPO=/path/to/repo" && exit 2)
	$(REV) $@ "$(REPO)" $(if $(filter-out index,$@),--out "$(OUT)") --indexer-dir "$(INDEXER)"

clean:
	rm -rf .graph $(OUT)
