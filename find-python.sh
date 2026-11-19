#!/bin/sh
# Print the command that runs rev-tool: the venv's python, else the interpreter that pip3 installs into.
# Usage: sh find-python.sh [pip-command]
PIP="${1:-pip3}"
ok() { [ -x "$1" ] || command -v "$1" >/dev/null 2>&1; }
has_tool() { "$1" -c "import rev_tool, ladybug" >/dev/null 2>&1; }

if [ -x .venv/bin/python ]; then echo ".venv/bin/python"; exit 0; fi

# the interpreter pip3 belongs to: shebang of the pip script, then the path in `pip --version`
pipbin=$(command -v "$PIP" 2>/dev/null || echo "$PIP")
cand=""
if [ -f "$pipbin" ]; then
  she=$(head -1 "$pipbin" | sed -n 's/^#![[:space:]]*//p' | awk '{print $1}')
  case "$she" in */env) she=$(head -1 "$pipbin" | awk '{print $2}');; esac
  [ -n "$she" ] && cand="$she"
fi
ver=$("$PIP" --version 2>/dev/null | sed -n 's/.* from \(.*\)\/lib\/.*\/site-packages.*(python \([0-9.]*\)).*/\1\/bin\/python\2/p')

for c in $cand $ver python3 python; do
  if ok "$c" && has_tool "$c"; then echo "$c"; exit 0; fi
done
for c in $cand $ver python3 python; do
  if ok "$c"; then echo "$c"; exit 0; fi
done
echo python3
