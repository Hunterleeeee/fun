#!/usr/bin/env sh
set -eu

REPO_URL="${FUN_REPO_URL:-https://github.com/Hunterleeeee/fun.git}"
FUN_REF="${FUN_VERSION:-v1.0.0a6}"
INSTALL_ROOT="${FUN_INSTALL_ROOT:-$HOME/.fun}"
VENV="$INSTALL_ROOT/venv"
BIN_DIR="${FUN_BIN_DIR:-$HOME/.local/bin}"

find_python() {
  for candidate in "${FUN_PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  echo "Fun requires Python 3.11 or newer." >&2
  echo "Install Python 3.11+ and run this installer again." >&2
  exit 1
fi

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install --upgrade "git+${REPO_URL}@${FUN_REF}"

ln -sf "$VENV/bin/fun" "$BIN_DIR/fun"

case ":${PATH}:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "Add Fun to PATH with:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo "Fun installed successfully."
echo "Run: fun"
