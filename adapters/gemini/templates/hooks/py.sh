#!/usr/bin/env bash
# Resolve a working Python interpreter and exec the given hook or command
# script.
#
# Hook subprocesses run through the shell with an arbitrary environment. Gemini
# does not know the script's interpreter, so this shim probes for a usable one:
# "python3" on plain POSIX installs, "python" on Windows, and "py" as the
# Windows launcher fallback. stdout stays exclusively the Python script's JSON
# response (the hook contract); diagnostics go to stderr.
set -u

SCRIPT="$1"
shift

PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
elif command -v py >/dev/null 2>&1; then
  PY="py"
fi

if [ -z "$PY" ]; then
  echo "mnemoseed: no Python interpreter found on PATH (python3/python/py)" >&2
  exit 0
fi

# The hook contract is JSON/text over UTF-8; a locale code page on the child's
# stdio would garble Chinese memory text before the parent reads it.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

exec "$PY" "$SCRIPT" "$@"
