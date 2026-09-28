#!/usr/bin/env bash
# ============================================================
#  Ningsi Studio launcher (Linux / macOS)
#  Usage:  ./run.sh [serve|demo|test|check|doctor|bootstrap] [args...]
#  Examples:
#    ./run.sh serve --port 8765
#    ./run.sh demo --participant p01 --speed 0.05
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING="utf-8"

resolve_python() {
    if [ -x "$VENV_PY" ]; then echo "$VENV_PY"; return; fi
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then echo "$candidate"; return; fi
    done
    echo ""
}

PYTHON="$(resolve_python)"
ACTION="${1:-menu}"
shift || true

step() { printf '  -> %s\n' "$1"; }

bootstrap() {
    step "prepare environment (.venv + dependencies)"
    local base
    base="$(resolve_python)"
    if [ -z "$base" ]; then
        echo "  [x] python3 not found; install Python 3.11+ first" >&2
        exit 2
    fi
    if [ ! -x "$VENV_PY" ]; then
        "$base" -m venv "$ROOT/.venv"
    fi
    "$VENV_PY" -m pip install --quiet --upgrade pip
    for engine in "$ROOT/../ningsi" "$ROOT/../ningsi/ningsi" "$ROOT/vendor/ningsi"; do
        if [ -f "$engine/pyproject.toml" ]; then
            step "pip install -e $engine"
            "$VENV_PY" -m pip install --quiet -e "$engine" || true
            break
        fi
    done
    "$VENV_PY" -c "import ningsi" >/dev/null 2>&1 || {
        step "pip install ningsi (PyPI)"
        "$VENV_PY" -m pip install --quiet ningsi || true
    }
    "$VENV_PY" -m pip install --quiet -e "$ROOT"
    PYTHON="$VENV_PY"
    "$PYTHON" -m ningsi_studio doctor || true
}

ensure_engine() {
    if [ -z "$PYTHON" ]; then
        echo "  [x] python3 not found; run ./run.sh bootstrap" >&2
        exit 2
    fi
    if ! "$PYTHON" -c "import ningsi, ningsi_studio" >/dev/null 2>&1; then
        for candidate in "$ROOT/../ningsi/src" "$ROOT/../ningsi/ningsi/src" "$ROOT/vendor/ningsi/src"; do
            if [ -f "$candidate/ningsi/__init__.py" ]; then
                export PYTHONPATH="$candidate:$PYTHONPATH"
                if "$PYTHON" -c "import ningsi, ningsi_studio" >/dev/null 2>&1; then
                    step "engine imported from source: $candidate"
                    return
                fi
            fi
        done
        echo "  [!] 'ningsi' engine is not importable; run ./run.sh bootstrap" >&2
        exit 2
    fi
}

case "$ACTION" in
    bootstrap)
        bootstrap
        ;;
    doctor)
        ensure_engine; "$PYTHON" -m ningsi_studio doctor
        ;;
    check)
        ensure_engine; "$PYTHON" -m ningsi_studio check
        ;;
    demo)
        ensure_engine; "$PYTHON" -m ningsi_studio demo "$@"
        ;;
    test)
        ensure_engine; cd "$ROOT" && "$PYTHON" -m unittest discover -s tests -t .
        ;;
    smoke)
        ensure_engine; bash "$ROOT/scripts/smoke.sh" "$@"
        ;;
    serve | "")
        ensure_engine
        echo ""
        echo "  open in browser: http://127.0.0.1:8765/"
        echo "  stop:            Ctrl+C"
        echo ""
        "$PYTHON" -m ningsi_studio serve "$@"
        ;;
    menu)
        ensure_engine
        cat <<'MENU'

  Ningsi Studio - A09 focus training & state assessment
  [1] serve    start web service (frontend + API)
  [2] demo     run one full session headless in fast mode
  [3] test     run the automated test suite
  [4] check    static self-check
  [5] smoke    smoke-test a RUNNING service
  [6] doctor   environment self-check
  [7] bootstrap prepare environment
  [0] quit
MENU
        printf '  choose: '
        read -r pick
        case "$pick" in
            1) exec "$0" serve ;;
            2) exec "$0" demo ;;
            3) exec "$0" test ;;
            4) exec "$0" check ;;
            5) exec "$0" smoke ;;
            6) exec "$0" doctor ;;
            7) exec "$0" bootstrap ;;
            *) echo "  cancelled" ;;
        esac
        ;;
    *)
        echo "unknown action: $ACTION" >&2
        echo "usage: ./run.sh [serve|demo|test|check|smoke|doctor|bootstrap]" >&2
        exit 1
        ;;
esac
