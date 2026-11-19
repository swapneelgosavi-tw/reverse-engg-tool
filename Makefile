PYTHON  ?= python3
VENV    := .venv
REV     := $(VENV)/bin/rev-tool
OUT     ?= output
INDEXER ?= ../reverselens/indexer
JAR     := $(INDEXER)/app/build/libs/reverselens-indexer.jar

.PHONY: build index generate all clean help

help:
	@echo "make build                  one-time setup (python venv + deps + indexer jar check)"
	@echo "make index    REPO=<path>   parse repo into LadybugDB graph"
	@echo "make generate REPO=<path>   write sequence diagrams + API request/response JSON to $(OUT)/"
	@echo "make all      REPO=<path>   index + generate"

build:
	@$(PYTHON) -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'python 3.11+ required; pass PYTHON=/path/to/python3')"
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet -e .
	@test -f $(JAR) || (echo "indexer jar missing; building..." && cd $(INDEXER) && ./install.sh)
	@echo "ready: $(REV)"

index generate all:
	@test -n "$(REPO)" || (echo "usage: make $@ REPO=/path/to/repo" && exit 2)
	$(REV) $@ "$(REPO)" $(if $(filter-out index,$@),--out "$(OUT)") --indexer-dir "$(INDEXER)"

clean:
	rm -rf .graph $(OUT)
