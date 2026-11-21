"""Build-time pytest plugin: per-test line coverage and file reads.

Runs only inside `docker build`, never when scoring. Uses sys.monitoring
(Python 3.12+), which is cheap: each line reports once per phase.

Hits are recorded under a key per phase:
  collect::<test file>   lines run while importing/collecting that test file
  setup::<nodeid>        fixture/class setup for a test
  call::<nodeid>         the test body
  teardown::<nodeid>     teardown
  boot                   anything before collection starts
"""
import concurrent.futures as _cf
import json
import os
import sys

import pytest

mon = sys.monitoring
TOOL = 3
ROOT = os.path.realpath(os.environ["COV_ROOT"])
OUT_DIR = os.environ["COV_OUT_DIR"]

_key = "boot"
_hits = {}
_opens = {}
_inst = {}           # key -> set of classes instantiated (via a Python __init__)


class _InlineExecutor:
    """Process pools run work in another process, invisible to coverage.
    Run it inline instead, with the same pickling a real pool does (pickling
    runs sqlglot code too) and the pool's initializer."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*_roundtrip(tuple(initargs)))

    def submit(self, fn, *a, **k):
        f = _cf.Future()
        try:
            fn2, a2, k2 = _roundtrip((fn, a, k))
            f.set_result(_roundtrip(fn2(*a2, **k2)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return [self.submit(fn, *args).result() for args in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _roundtrip(obj):
    import pickle
    try:
        return pickle.loads(pickle.dumps(obj))
    except Exception:  # noqa: BLE001
        return obj


_class_lines = {}


def _class_starts(f):
    """Line numbers where class bodies begin (class keyword and decorators)."""
    r = _class_lines.get(f)
    if r is None:
        import ast
        r = set()
        try:
            tree = ast.parse(open(f, "rb").read())
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    r.add(n.lineno)
                    r.update(d.lineno for d in n.decorator_list)
        except Exception:  # noqa: BLE001
            pass
        _class_lines[f] = r
    return r


def _line(code, line):
    f = code.co_filename
    if f.startswith(ROOT):
        # Module and class bodies run once at import; who triggers the import
        # is an accident of test order, so they are not recorded. Changes to
        # them are handled by name at selection time.
        if code.co_name == "<module>" or code.co_firstlineno in _class_starts(f):
            return mon.DISABLE
        s = _hits.get(_key)
        if s is None:
            s = _hits[_key] = set()
        s.add((f[len(ROOT) + 1:], line))
    return mon.DISABLE


def _audit(ev, args):
    if ev != "open":
        return
    p = args[0]
    if isinstance(p, int):
        return
    try:
        ap = os.path.realpath(os.fsdecode(p))
    except Exception:  # noqa: BLE001
        return
    # .py files are opened by the import system, which says nothing about
    # what a test depends on; line coverage handles code.
    if ap.startswith(ROOT) and not ap.endswith((".py", ".pyc")):
        _opens.setdefault(_key, set()).add(ap[len(ROOT) + 1:])


def _start(code, offset, depth=1):
    if code.co_name != "__init__" or not code.co_filename.startswith(ROOT):
        return mon.DISABLE if code.co_name != "<module>" else None
    try:
        f = sys._getframe(depth)
        if code.co_argcount:
            obj = f.f_locals.get(code.co_varnames[0])
            if obj is not None:
                s = _inst.get(_key)
                if s is None:
                    s = _inst[_key] = set()
                s.add(type(obj))
    except Exception:  # noqa: BLE001
        pass
    return None


_stack = []


def _module_start(code, offset):
    """Entering a module body in the repo: attribute what runs during the
    import to the module being imported, not to whichever test caused it."""
    global _key
    if code.co_name == "<module>" and code.co_filename.startswith(ROOT):
        rel = code.co_filename[len(ROOT) + 1:]
        _stack.append(_key)
        _key = ("collect::" if rel.startswith("tests/") else "import::") + rel
        mon.restart_events()
        return None
    return _start(code, offset, 2)


def _module_end(code, *args):
    global _key
    if code.co_name == "<module>" and code.co_filename.startswith(ROOT):
        if _stack:
            _key = _stack.pop()
            # lines switched off during the import must be seen again by the test
            mon.restart_events()
        return None
    return mon.DISABLE


def _module_unwind(code, *args):
    # PY_UNWIND cannot be disabled; only act on module bodies
    if code.co_name == "<module>":
        _module_end(code)
    return None


def _set(key):
    global _key
    _key = key
    mon.restart_events()


def pytest_configure(config):
    _cf.ProcessPoolExecutor = _InlineExecutor
    mon.use_tool_id(TOOL, "covmap")
    mon.register_callback(TOOL, mon.events.LINE, _line)
    mon.register_callback(TOOL, mon.events.PY_START, _module_start)
    mon.register_callback(TOOL, mon.events.PY_RETURN, _module_end)
    mon.register_callback(TOOL, mon.events.PY_UNWIND, _module_unwind)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START | mon.events.PY_RETURN | mon.events.PY_UNWIND)
    sys.addaudithook(_audit)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    path = getattr(collector, "path", None)
    if isinstance(collector, pytest.Module) and path is not None:
        prev = _key
        _set("collect::" + os.path.relpath(os.path.realpath(path), ROOT))
        yield
        _set(prev)
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set("setup::" + item.nodeid)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set("call::" + item.nodeid)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _set("teardown::" + item.nodeid)
    yield
    _set("between")


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    out = {"hits": {}, "opens": {}}
    for k, s in _hits.items():
        d = {}
        for f, ln in s:
            d.setdefault(f, []).append(ln)
        out["hits"][k] = d
    out["opens"] = {k: sorted(v) for k, v in _opens.items()}
    names = {}

    def mro_names(cls):
        r = names.get(cls)
        if r is None:
            r = names[cls] = [f"{c.__module__}.{c.__qualname__}" for c in cls.__mro__
                               if c is not object]
        return r

    out["inst"] = {k: sorted({n for c in v for n in mro_names(c)}) for k, v in _inst.items()}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"cov-{os.getpid()}.json"), "w") as fh:
        json.dump(out, fh)
