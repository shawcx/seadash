#!/bin/sh
# Runs in the container (see check.sh), with the source arriving as a tar on stdin.
cd /work && tar x
/venv/bin/pip install -q -e . >/dev/null 2>&1
echo "== tests ($(g++-14 --version | head -1), $(python3 --version))"
/venv/bin/python -m pytest -q -p no:cacheprovider tests/ 2>&1 | tail -1

echo "== -Wall -Wextra"
mkdir -p /tmp/w
ls tests/programs/*.sd | xargs -P"$(nproc)" -I{} sh -c '
    n=$(basename {} .sd)
    /venv/bin/sd emit {} > /tmp/w/$n.cpp 2>/dev/null || exit 0
    g++-14 -std=c++23 -fwrapv -ffp-contract=off -Wall -Wextra -fsyntax-only -Iseadash/runtime /tmp/w/$n.cpp 2> /tmp/w/$n.err ||
        echo "does not compile: $n"
    g++-14 -std=c++23 -fwrapv -ffp-contract=off -DSD_TRACEBACK -Wall -Wextra -fsyntax-only -Iseadash/runtime /tmp/w/$n.cpp \
        2>> /tmp/w/$n.err || echo "does not compile with tracebacks: $n"'
warned=$(grep -l "warning:" /tmp/w/*.err 2>/dev/null | wc -l)
echo "$(ls /tmp/w/*.cpp | wc -l) programs, $warned with warnings"
grep -h "warning:" /tmp/w/*.err 2>/dev/null | sed -E 's/^[^ ]*: warning: //' | sort | uniq -c | sort -rn | head -20

[ "$1" = "--sanitize" ] || exit 0

run() {  # run SANITIZER PROGRAM: build it with the sanitizer, run it, compare with its .out
    san=$1 n=$2 f=tests/programs/$2.sd
    libs=$(/venv/bin/python -c "from pathlib import Path; from seadash.driver import translate
print(' '.join('-l' + l for l in translate(Path('$f').read_text(), Path('$f')).libs))")
    /venv/bin/sd emit "$f" > /tmp/$n.cpp
    # (with the debug build's tracebacks, which keep frames on the stack: more to check)
    if ! g++-14 -std=c++23 -fwrapv -ffp-contract=off -DSD_TRACEBACK -g -O1 -fsanitize=$san -Iseadash/runtime /tmp/$n.cpp \
            -o /tmp/$n.$san $libs 2> /tmp/$n.build; then
        echo "$san $n: does not build"; return
    fi
    d=$(mktemp -d)
    cp -r tests/certs "$d/certs"  # (the HTTPS programs' certificate)
    if [ "$san" = thread ]; then
        (cd "$d" && TSAN_OPTIONS=halt_on_error=1 setarch "$(uname -m)" -R timeout 120 /tmp/$n.$san > out 2> err)
    else
        (cd "$d" && timeout 120 /tmp/$n.$san > out 2> err)
    fi
    reports=$(grep -cE "Sanitizer|runtime error" "$d/err")
    if [ "$reports" != 0 ]; then
        echo "$san $n: $reports reports"; grep -m3 -E "Sanitizer|runtime error" "$d/err"
    elif ! cmp -s "$d/out" "tests/programs/$n.out"; then
        echo "$san $n: output differs"
    fi
}
echo "== address,undefined"
for f in tests/programs/*.sd; do run address,undefined "$(basename "$f" .sd)"; done
echo "== thread"
for f in tests/programs/threads*.sd tests/programs/http_server*.sd tests/programs/http_threads*.sd tests/programs/futures*.sd tests/programs/signal*.sd; do
    run thread "$(basename "$f" .sd)"
done
echo "(no news is good news)"
