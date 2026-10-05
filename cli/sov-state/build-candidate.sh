#!/bin/sh
# Build a candidate beside the eventual binary, without touching an installed version.
set -eu

if [ "$#" -ne 1 ]; then
    >&2 printf 'usage: sh build-candidate.sh /absolute/path/to/candidate\n'
    exit 2
fi
case "$1" in
    /*) candidate=$1 ;;
    *) >&2 printf 'candidate path must be absolute\n'; exit 2 ;;
esac
if [ -e "$candidate" ] || [ -L "$candidate" ]; then
    >&2 printf 'candidate already exists: %s\n' "$candidate"
    exit 2
fi
dir=$(dirname -- "$candidate")
source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
tmp=$(mktemp "$dir/.sov-state-build.XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

(cd "$source" && go build -trimpath -o "$tmp" .)
version=$("$tmp" --version)
case "$version" in
    *'"v":1'*'"message":"sov-state 0.3.0 (protocol v:1)"'*) ;;
    *) >&2 printf 'unexpected sov-state version: %s\n' "$version"; exit 1 ;;
esac
mv -- "$tmp" "$candidate"
printf '%s\n' "$version"
