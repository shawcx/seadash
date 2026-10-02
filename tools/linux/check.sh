#!/bin/sh
# Checks seadash on Linux with g++-14, in Docker (for when you're on macOS):
# the test suite, -Wall -Wextra on every test program, and with --sanitize, the
# programs under ASan/UBSan and the thread programs under TSan.
#
#   tools/linux/check.sh              # the working tree: uncommitted changes and new files included
#   tools/linux/check.sh --sanitize
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
docker build -q -t seadash-linux "$here" >/dev/null
# The working tree as git would commit it (new files too, .gitignore'd ones not), staged in a
# throwaway index so the real one isn't touched.
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
export GIT_INDEX_FILE="$scratch/index"
git -C "$root" read-tree HEAD
git -C "$root" add -A
tree=$(git -C "$root" write-tree)
unset GIT_INDEX_FILE
git -C "$root" archive "$tree" |
    docker run --rm -i --security-opt seccomp=unconfined -v "$here:/tools:ro" seadash-linux sh /tools/inside.sh "$@"
