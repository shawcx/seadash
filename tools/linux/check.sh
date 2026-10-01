#!/bin/sh
# Checks seadash on Linux with g++-14, in Docker (for when you're on macOS):
# the test suite, -Wall -Wextra on every test program, and with --sanitize, the
# programs under ASan/UBSan and the thread programs under TSan.
#
#   tools/linux/check.sh              # the working tree, uncommitted changes included
#   tools/linux/check.sh --sanitize
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
docker build -q -t seadash-linux "$here" >/dev/null
rev=$(git -C "$root" stash create)  # (a commit of the working tree; HEAD if nothing changed)
git -C "$root" archive "${rev:-HEAD}" |
    docker run --rm -i --security-opt seccomp=unconfined -v "$here:/tools:ro" seadash-linux sh /tools/inside.sh "$@"
