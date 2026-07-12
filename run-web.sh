#!/usr/bin/env bash
# Launch the local Resume Builder web UI.
#
# First run sets everything up for you (creates a private Python environment and
# installs dependencies — about 30 seconds, once). Every run after that just
# launches and opens your browser.
#
# Works from any directory:   ./run-web.sh   OR   /full/path/to/run-web.sh
# Don't auto-open a browser:  RESUME_BUILDER_NO_BROWSER=1 ./run-web.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"
REQS="$ROOT/requirements.txt"
STAMP="$VENV/.deps-installed"   # copy of the requirements.txt we last installed
MIN_PY_MAJOR=3
MIN_PY_MINOR=9

say()  { printf '\033[36m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Find a python3 new enough to build the virtualenv (only needed on first run).
find_python() {
  local cand
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)" >/dev/null 2>&1; then
      printf '%s' "$cand"
      return 0
    fi
  done
  return 1
}

# True if the core (non-optional) dependencies import cleanly.
core_ok() { "$VENV_PY" -c 'import flask, docx, yaml, pydantic, pypdf' >/dev/null 2>&1; }

# True if we already installed exactly this requirements.txt.
stamp_matches() { [ -f "$STAMP" ] && cmp -s "$REQS" "$STAMP"; }

install_deps() {  # $1 = "fatal" if core deps MUST end up importable
  say "Installing dependencies (one moment)…"
  "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  if "$VENV_PY" -m pip install -r "$REQS"; then
    cp "$REQS" "$STAMP"
    say "Dependencies ready."
    return 0
  fi
  # pip failed — that's fine as long as the *core* app can still run. Optional
  # extras (e.g. the Anthropic SDK) aren't required: the claude CLI and
  # copy-paste paths work without them.
  if core_ok; then
    warn "Some optional dependencies didn't install — the app still works (copy-paste / claude CLI). Re-run later to retry."
    cp "$REQS" "$STAMP"
    return 0
  fi
  [ "${1:-}" = "fatal" ] \
    && die "Dependency install failed. Run it manually:  \"$VENV_PY\" -m pip install -r \"$REQS\""
  die "Core dependencies are missing. Run it manually:  \"$VENV_PY\" -m pip install -r \"$REQS\""
}

# --- 1. Create the virtualenv on first run -------------------------------------
if [ ! -x "$VENV_PY" ]; then
  say "First-time setup — creating a private Python environment…"
  if ! PYBIN="$(find_python)"; then
    die "Need Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+. Install it, then re-run ./run-web.sh
     macOS:   brew install python     (or: xcode-select --install)
     Ubuntu:  sudo apt install python3 python3-venv
     Other:   https://www.python.org/downloads/"
  fi
  say "Using $("$PYBIN" --version 2>&1)."
  "$PYBIN" -m venv "$VENV" \
    || die "Could not create the environment. On Debian/Ubuntu try:  sudo apt install python3-venv"
  install_deps fatal
# --- 2. Existing venv: install only if something's missing or changed ----------
elif ! core_ok || ! stamp_matches; then
  install_deps
fi

# --- 3. Launch -----------------------------------------------------------------
cd "$ROOT"
exec env PYTHONPATH="$ROOT/src" "$VENV_PY" -m resume_builder.web "$@"
