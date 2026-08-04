#!/bin/sh
set -eu

REPO_URL=${SOL_REPO_URL:-https://github.com/bytonylee/solar-code/archive/refs/heads/main.tar.gz}
INSTALL_ROOT=${SOL_INSTALL_ROOT:-"$HOME/.local/share/solar-code"}
BIN_DIR=${SOL_BIN_DIR:-"$HOME/.local/bin"}

case "$INSTALL_ROOT" in
    ""|"/")
        printf '%s\n' 'SOL_INSTALL_ROOT must point to a dedicated directory.' >&2
        exit 1
        ;;
esac

if ! command -v curl >/dev/null 2>&1; then
    printf '%s\n' 'sol installer requires curl.' >&2
    exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
    printf '%s\n' 'sol installer requires tar.' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'sol requires Python 3.10+ available as python3.' >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    printf '%s\n' 'sol requires Python 3.10 or newer.' >&2
    exit 1
fi

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT INT TERM
archive="$tmp_dir/sol.tar.gz"

curl -fsSL "$REPO_URL" -o "$archive"
tar -xzf "$archive" -C "$tmp_dir"
source_dir=$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d -print -quit)
if [ -z "$source_dir" ]; then
    printf '%s\n' 'sol installer could not find the source archive.' >&2
    exit 1
fi

rm -rf "$INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
cp -R "$source_dir/bin" "$source_dir/src" "$source_dir/.env.example" "$INSTALL_ROOT/"
ln -sfn "$INSTALL_ROOT/bin/sol" "$BIN_DIR/sol"

printf 'Installed sol in %s\n' "$INSTALL_ROOT"
if ! printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
    printf 'Add this directory to PATH: %s\n' "$BIN_DIR"
fi
printf '%s\n' 'Run: sol --help'
