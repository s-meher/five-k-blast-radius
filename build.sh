#!/bin/sh
# Build-time only: fetch sqlglot at the pinned commit, run its tests once with
# per-test coverage, and compress the result into /app/map.marshal.
set -eu
APP=${APP:-/app}
SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

if [ ! -d "$APP/src/sqlglot" ]; then
  python - "$APP" "$SHA" <<'PY'
import io, sys, tarfile, urllib.request, shutil, os
app, sha = sys.argv[1], sys.argv[2]
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as t:
    t.extractall(app, filter="data")
shutil.move(os.path.join(app, f"sqlglot-{sha}"), os.path.join(app, "src"))
PY
fi

cd "$APP/src"
COV=/tmp/cov
rm -rf "$COV"
export SKIP_INTEGRATION=0 COV_ROOT="$APP/src" COV_OUT_DIR="$COV" PYTHONPATH="$APP/src:$APP" PYTHONDONTWRITEBYTECODE=1

# five groups of similar cost, run side by side (the build has 4 cores)
ALL=$(find tests -name 'test_*.py' | sort)
REST=$(echo "$ALL" | grep -v -e '^tests/test_executor.py$' -e '^tests/test_parser.py$' -e '^tests/test_optimizer.py$')
# split the remaining files into two halves by size
HALF_A=$(for f in $REST; do echo "$(wc -c < $f) $f"; done | sort -rn | awk 'NR%2==1 {print $2}' | tr '\n' ' ')
HALF_B=$(for f in $REST; do echo "$(wc -c < $f) $f"; done | sort -rn | awk 'NR%2==0 {print $2}' | tr '\n' ' ')
pids=""
i=0
for g in "tests/test_executor.py" "tests/test_parser.py" "tests/test_optimizer.py" "$HALF_A" "$HALF_B"; do
  i=$((i+1))
  python -m pytest -q -p covplugin -p no:cacheprovider $g > /tmp/pytest-group$i.log 2>&1 &
  pids="$pids $!"
done
fail=0
for p in $pids; do
  rc=0; wait "$p" || rc=$?
  # 0: all passed, 1: some tests failed (fine for a map); anything else means pytest broke
  if [ "$rc" -gt 1 ]; then echo "test group exited with $rc"; fail=1; fi
done
if [ "$fail" -ne 0 ]; then tail -n 20 /tmp/pytest-group*.log; exit 1; fi
tail -n 2 /tmp/pytest-*.log || true

python "$APP/build_map.py" "$COV" "$APP/map.marshal" "$APP/src" "$i"
rm -rf "$COV"
find "$APP/src" -name '__pycache__' -prune -exec rm -rf {} +
chmod -R a+rX "$APP"
