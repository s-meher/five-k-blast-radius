"""Check a change by running tests inside this container.

The parent process imports sqlglot (and the test modules it will need) once.
For each change, it forks; the child swaps in the changed functions' new code
objects, runs the chosen tests with unittest, and reports which failed.

Only used for changes confined to function bodies in sqlglot/*.py whose
functions never run while modules are being imported, so the live process
behaves exactly like a fresh import of the patched source.
"""

import concurrent.futures as cf
import gc
import io
import json
import os
import pickle
import select
import signal
import sys
import time
import types
import unittest

# Tests that would see the unpatched files on disk (a subprocess, a re-import,
# reading source text). These are never checked here, only selected.
UNSAFE_FILES = {
    "tests/test_typing.py", "tests/test_docs.py", "tests/test_sqlglotc.py",
    "tests/test_dialect_imports.py", "tests/leakcheck.py",
}
UNSAFE_TESTS = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}


def log(*a):
    print("verify:", *a, file=sys.stderr)


def modname(path):
    m = path[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def safe_test(tid):
    return tid.split("::", 1)[0] not in UNSAFE_FILES and tid not in UNSAFE_TESTS


class InlineExecutor:
    """Process pools would start fresh interpreters that import the unpatched
    files; run pool work inline instead, pickling like a real pool does."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*_roundtrip(tuple(initargs)))

    def submit(self, fn, *a, **k):
        f = cf.Future()
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
    return pickle.loads(pickle.dumps(obj))


class Warm:
    def __init__(self, src_root):
        self.src = str(src_root)
        self.ready = False

    def prepare(self, modules):
        t = time.process_time()
        sys.dont_write_bytecode = True
        if self.src not in sys.path:
            sys.path.insert(0, self.src)
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        import importlib
        import sqlglot  # noqa: F401
        for m in sorted(modules):
            try:
                importlib.import_module(m)
            except Exception as e:  # noqa: BLE001
                log("cannot import", m, repr(e))
                return False
        cf.ProcessPoolExecutor = InlineExecutor
        for name, mod in list(sys.modules.items()):
            if name.startswith("tests") and getattr(mod, "ProcessPoolExecutor", None) is not None:
                mod.ProcessPoolExecutor = InlineExecutor
        gc.collect()
        self.ready = True
        log(f"warm in {time.process_time() - t:.2f}s cpu")
        return True

    # ---------------------------------------------------------------- child
    def _swap(self, path, new_src, quals):
        mod = sys.modules.get(modname(path))
        if mod is None:
            raise RuntimeError(f"module not loaded: {path}")
        filename = mod.__file__
        code = compile(new_src, filename, "exec", dont_inherit=True)
        found = {}
        stack = [code]
        while stack:
            c = stack.pop()
            for k in c.co_consts:
                if isinstance(k, types.CodeType):
                    found.setdefault(k.co_qualname, []).append(k)
                    stack.append(k)
        for qual in quals:
            news = found.get(qual, [])
            if len(news) != 1:
                raise RuntimeError(f"new code for {qual}: {len(news)}")
            new = news[0]
            funcs = self._live(mod, qual, filename)
            if not funcs:
                raise RuntimeError(f"no live function for {qual}")
            for f in funcs:
                if f.__code__.co_freevars != new.co_freevars:
                    raise RuntimeError(f"closure changed in {qual}")
                f.__code__ = new

    def _live(self, mod, qual, filename):
        """Every function object running the old code of `qual`."""
        out = []
        seen = set()
        for o in gc.get_objects():
            if type(o) is types.FunctionType:
                c = o.__code__
                if c.co_qualname == qual and c.co_filename == filename and id(o) not in seen:
                    seen.add(id(o))
                    out.append(o)
        return out

    def _run(self, test_ids):
        suite = unittest.TestSuite()
        for tid in test_ids:
            file, cls, meth = tid.split("::")
            mod = sys.modules[modname(file)]
            suite.addTest(getattr(mod, cls)(meth))
        result = unittest.TestResult()
        suite.run(result)
        failed = set()
        whole = False
        for test, _ in result.failures + result.errors + [(t, None) for t in result.unexpectedSuccesses]:
            case = getattr(test, "test_case", test)
            tid = case.id() if hasattr(case, "id") else ""
            parts = tid.rsplit(".", 2)
            if len(parts) == 3 and isinstance(case, unittest.TestCase):
                failed.add(parts)
            else:
                whole = True   # class/module setup failed: count everything
        if whole:
            return list(test_ids)
        out = []
        for tid in test_ids:
            file, cls, meth = tid.split("::")
            if [modname(file), cls, meth] in [list(p) for p in failed]:
                out.append(tid)
        # every test must have been run
        if result.testsRun < len(test_ids):
            return list(test_ids)
        return out

    def check(self, changes, test_ids, timeout):
        """changes: [(path, new_src, [qualnames])]. Returns failing ids, or
        None when the check could not be done."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(r)
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", closefd=False))
                for path, new_src, quals in changes:
                    self._swap(path, new_src, quals)
                failing = self._run(test_ids)
                msg = json.dumps({"ok": True, "failing": failing})
            except BaseException as e:  # noqa: BLE001
                msg = json.dumps({"ok": False, "error": repr(e)[:300]})
            os.write(w, msg.encode())
            os.close(w)
            os._exit(0)
        os.close(w)
        deadline = time.monotonic() + timeout
        buf = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                os.close(r)
                log("timeout")
                return list(test_ids)       # treat as failing: they get selected
            ready, _, _ = select.select([r], [], [], left)
            if ready:
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                buf += chunk
        os.close(r)
        os.waitpid(pid, 0)
        try:
            msg = json.loads(buf.decode())
        except Exception:  # noqa: BLE001
            return None
        if not msg.get("ok"):
            log("check failed:", msg.get("error"))
            return None
        return msg["failing"]

    def import_ok(self, path, new_src):
        """Run the patched module's top level in a fresh namespace (in a
        child) to catch import-time errors. None if it cannot be judged."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            ok = b"0"
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                mod = sys.modules.get(modname(path))
                name = modname(path)
                ns = {"__name__": name, "__file__": getattr(mod, "__file__", path),
                      "__package__": name.rpartition(".")[0] if not path.endswith("__init__.py") else name,
                      "__builtins__": __builtins__}
                exec(compile(new_src, ns["__file__"], "exec", dont_inherit=True), ns)
                ok = b"1"
            except BaseException:  # noqa: BLE001
                ok = b"0"
            os.write(w, ok)
            os.close(w)
            os._exit(0)
        os.close(w)
        ready, _, _ = select.select([r], [], [], 30)
        data = os.read(r, 1) if ready else b""
        if not ready:
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(r)
        return data == b"1"
