PYTHON  ?= python3
PIP     ?= pip3
VENV    := .venv
# runs `python -m rev_tool`: the venv's python, else the interpreter pip3 installs into (no PATH setup needed)
REV     ?= $(shell sh find-python.sh $(PIP)) -m rev_tool
OUT     ?= output

.PHONY: build build-venv build-pip index generate all clean help

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
	@echo "ready: $(VENV)/bin/python -m rev_tool"

# non-editable: old pip (<21.3) cannot do editable installs of pyproject-only projects; re-run after changing rev_tool/
build-pip:
	$(PIP) install --upgrade .
	@echo "ready: $(REV)"

index generate all:
	@test -n "$(REPO)" || (echo "usage: make $@ REPO=/path/to/repo" && exit 2)
	$(REV) $@ "$(REPO)" $(if $(filter-out index,$@),--out "$(OUT)")

clean:
	rm -rf .graph $(OUT)
