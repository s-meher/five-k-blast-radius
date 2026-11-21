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

# four groups, run side by side (the build has 4 cores)
ALL=$(find tests -name 'test_*.py' | sort)
G1="tests/test_executor.py"
G2="tests/test_parser.py"
G3="tests/test_optimizer.py"
G4=$(echo "$ALL" | grep -v -e '^tests/test_executor.py$' -e '^tests/test_parser.py$' -e '^tests/test_optimizer.py$' | tr '\n' ' ')
pids=""
for g in "$G1" "$G2" "$G3" "$G4"; do
  python -m pytest -q -p covplugin -p no:cacheprovider $g > /tmp/pytest-$(echo "$g" | cut -c1-20 | tr '/ ' '__').log 2>&1 &
  pids="$pids $!"
done
for p in $pids; do wait "$p" || true; done
tail -n 2 /tmp/pytest-*.log || true

python "$APP/build_map.py" "$COV" "$APP/map.marshal" "$APP/src"
rm -rf "$COV"
find "$APP/src" -name '__pycache__' -prune -exec rm -rf {} +
chmod -R a+rX "$APP"
