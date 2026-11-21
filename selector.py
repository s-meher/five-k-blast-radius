"""Blast Radius selector.

For each patch, pick the tests that could fail and split them over workers.

How a change maps to tests (all maps come from the pinned commit, built in
the Dockerfile):
  * A Python change whose syntax tree is unchanged (comments, formatting)
    runs nothing.
  * A changed line inside a function or lambda selects the tests that ran
    that line (per-test coverage).
  * A changed module- or class-level statement (constants, dict tables,
    imports, signatures) selects every test that ran a line that reads one of
    the names it binds. The patched module is also imported once to catch
    import-time errors.
  * A changed data file (fixtures) selects the tests that opened it.
  * Anything we cannot reason about selects every test.
A missed failure costs 300 s, so every rule errs on the side of more tests.
"""

import ast
import json
import marshal
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(os.environ.get("BR_APP", "/app"))
SRC = APP / "src"
WORKER_MS = 2000
MAX_WORKERS = 8

CONFIG_NAMES = {
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "conftest.py", "MANIFEST.in", "Makefile",
}


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------
# patches

class FilePatch:
    def __init__(self, old, new):
        self.old = old            # path in the pinned tree, or None if added
        self.new = new            # path after the change, or None if deleted
        self.hunks = []           # (old_start, [lines with +/-/space prefix])

    @property
    def path(self):
        return self.old or self.new


def parse_patch(text):
    files = []
    cur = None
    old_name = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = None
            old_name = None
        elif line.startswith("--- "):
            p = line[4:].split("\t")[0]
            old_name = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            new_name = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            cur = FilePatch(old_name, new_name)
            files.append(cur)
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m or cur is None:
                raise ValueError("bad hunk header")
            body = []
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1
                    continue
                tag = b[:1] if b else " "
                if tag == "-":
                    old_left -= 1
                elif tag == "+":
                    new_left -= 1
                elif tag == " ":
                    old_left -= 1
                    new_left -= 1
                else:
                    break
                body.append(b if b else " ")
                i += 1
            cur.hunks.append((int(m.group(1)), int(m.group(3)), body))
            continue
        elif line.startswith(("rename from", "rename to", "Binary files", "GIT binary patch")):
            if cur is None:
                files.append(FilePatch("<unknown>", "<unknown>"))
        i += 1
    return files


def apply_hunks(old_text, fp):
    """Old text -> new text, plus changed old lines, insertion neighbours and
    added new lines."""
    old = old_text.split("\n")
    out = []
    pos = 0                     # 0-based index into old
    removed, neighbours, added = set(), set(), set()
    for old_start, new_start, body in fp.hunks:
        start = max(old_start - 1, 0) if any(b[:1] in " -" for b in body) else old_start
        out.extend(old[pos:start])
        pos = start
        o = start + 1           # 1-based old line number of the next old line
        n = new_start
        for b in body:
            tag, content = b[:1], b[1:]
            if tag == " ":
                out.append(old[pos])
                pos += 1
                o += 1
                n += 1
            elif tag == "-":
                removed.add(o)
                pos += 1
                o += 1
            elif tag == "+":
                out.append(content)
                neighbours.update((o - 1, o))
                added.add(n)
                n += 1
    out.extend(old[pos:])
    return "\n".join(out), removed, neighbours, added


# --------------------------------------------------------------------------
# syntax helpers

def ast_dump(tree):
    return ast.dump(tree, include_attributes=False)


FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def locate(tree, line):
    """Where a line sits.

    Returns (kind, node, scope_qualname):
      ("body", stmt, qual)   inside a function/lambda body; stmt is the
                             innermost statement holding the line
      ("def", stmt, qual)    part of a module/class-level statement
      None                   between statements (blank or comment)
    """
    def visit(stmts, qual, in_func):
        for s in stmts:
            end = getattr(s, "end_lineno", s.lineno)
            first = min([s.lineno] + [d.lineno for d in getattr(s, "decorator_list", [])])
            if not (first <= line <= end):
                continue
            if isinstance(s, FUNCS):
                body_first = s.body[0].lineno
                # a docstring-only or first statement starting the body
                if line >= body_first:
                    r = visit(s.body, f"{qual}.{s.name}" if qual else s.name, True)
                    return r or ("body", s, qual)
                return ("body", s, qual) if in_func else ("def", s, qual)
            if isinstance(s, ast.ClassDef):
                if s.body and line >= s.body[0].lineno:
                    r = visit(s.body, f"{qual}.{s.name}" if qual else s.name, in_func)
                    return r or (("body", s, qual) if in_func else None)
                return ("body", s, qual) if in_func else ("def", s, qual)
            # compound statements: descend into their blocks
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                block = getattr(s, field, None)
                if isinstance(block, list) and block and isinstance(block[0], (ast.stmt, ast.excepthandler, ast.match_case)):
                    inner = []
                    for b in block:
                        if isinstance(b, (ast.excepthandler, ast.match_case)):
                            inner.extend(b.body)
                        else:
                            inner.append(b)
                    r = visit(inner, qual, in_func)
                    if r:
                        return r
            if in_func:
                return ("body", s, qual)
            # a lambda inside a module/class-level statement
            for n in ast.walk(s):
                if isinstance(n, ast.Lambda) and n.body.lineno <= line <= n.body.end_lineno:
                    return ("lambda", s, qual)
            return ("def", s, qual)
        return None

    return visit(tree.body, "", False)


def bound_names(stmt):
    names = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
        return names
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
        return names
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    if not targets:
        for n in ast.walk(stmt):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
    return names


def strip_lambdas(node):
    class S(ast.NodeTransformer):
        def visit_Lambda(self, n):
            return ast.Constant(value="<lambda>")
    return ast_dump(S().visit(ast.parse(ast.unparse(node))))


# --------------------------------------------------------------------------
# selection

class Selector:
    def __init__(self, tests):
        with open(APP / "map.marshal", "rb") as fh:
            m = marshal.load(fh)
        self.map_tests = m["tests"]
        masks = m.get("masks")
        if masks is not None:
            self.lines = {f: {ln: masks[i] for ln, i in d.items()} for f, d in m["lines"].items()}
        else:
            self.lines = m["lines"]
        self.opens = m["opens"]
        self.byfile = m["byfile"]
        self.classes = m.get("classes", {})
        self.tests = tests
        # map bit -> test number; tests unknown to the map are always run
        by_id = {t["id"]: t["number"] for t in tests}
        self.bit_number = [by_id.get(t) for t in self.map_tests]
        known = set(self.map_tests)
        self.unmapped = {t["number"] for t in tests if t["id"] not in known}
        self.all_numbers = {t["number"] for t in tests}
        self._src = {}
        self._name_cache = {}
        self._py_files = None

    # ---- helpers
    def src(self, path):
        if path not in self._src:
            p = SRC / path
            self._src[path] = p.read_text(encoding="utf-8") if p.is_file() else None
        return self._src[path]

    def mask_to_numbers(self, mask):
        out = set()
        i = 0
        while mask:
            if mask & 1:
                n = self.bit_number[i]
                if n is not None:
                    out.add(n)
            mask >>= 1
            i += 1
        return out

    def lines_mask(self, path, line_numbers):
        d = self.lines.get(path)
        if not d:
            return 0
        m = 0
        for ln in line_numbers:
            m |= d.get(ln, 0)
        return m

    def range_mask(self, path, first, last):
        d = self.lines.get(path)
        if not d:
            return 0
        m = 0
        for ln, v in d.items():
            if first <= ln <= last:
                m |= v
        return m

    def file_mask(self, path):
        m = 0
        for v in self.lines.get(path, {}).values():
            m |= v
        return m

    def py_files(self):
        if self._py_files is None:
            self._py_files = [p for p in self.lines if p.endswith(".py")]
        return self._py_files

    def name_users(self, name):
        """Tests that ran a line mentioning `name`."""
        if name in self._name_cache:
            return self._name_cache[name]
        rx = re.compile(r"\b" + re.escape(name) + r"\b")
        m = 0
        for path in self.py_files():
            text = self.src(path)
            if text is None or name not in text:
                continue
            d = self.lines[path]
            for i, line in enumerate(text.split("\n"), 1):
                if i in d and rx.search(line):
                    m |= d[i]
        self._name_cache[name] = m
        return m

    def walkers(self):
        """Tests that walk classes generically (every function, every subclass)."""
        if not hasattr(self, "_walkers"):
            self._walkers = (self.name_users("ALL_FUNCTIONS") | self.name_users("subclasses")
                             | self.name_users("__subclasses__"))
        return self._walkers

    def copiers(self, cls_name, names):
        """Classes whose bodies read `Cls.attr` at import time (copying a table)."""
        out = set()
        rx = re.compile(r"\b" + re.escape(cls_name) + r"(?:\.\w+)*\.(" + "|".join(map(re.escape, names)) + r")\b") if names else None
        if rx is None:
            return out
        for path in self.py_files():
            if not path.startswith("sqlglot/"):
                continue
            text = self.src(path)
            if not text or cls_name not in text or not rx.search(text):
                continue
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for i, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    loc = locate(tree, i)
                    if loc and loc[0] != "body" and loc[2]:
                        out.add(f"{module}.{loc[2]}")
                    elif loc and loc[0] != "body":
                        out.add("*module*")
        if "*module*" in out:
            out.discard("*module*")
            out.add("*")
        return out

    def def_refs(self, name):
        """Classes whose body wires `name` in by plain assignment
        (Generator = TableauGenerator), plus the classes those are nested in."""
        cache = self.__dict__.setdefault("_def_refs", {})
        if name in cache:
            return cache[name]
        rx = re.compile(r"\b" + re.escape(name) + r"\b")
        out = set()
        for path in self.py_files():
            if not path.startswith("sqlglot/"):
                continue
            text = self.src(path)
            if not text or name not in text:
                continue
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            tree = None
            for i, line in enumerate(text.split("\n"), 1):
                if not rx.search(line):
                    continue
                if tree is None:
                    try:
                        tree = ast.parse(text)
                    except SyntaxError:
                        break
                loc = locate(tree, i)
                if not (loc and loc[0] == "def" and loc[2]):
                    continue
                st = loc[1]
                val = getattr(st, "value", None)
                if not (isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(val, (ast.Name, ast.Attribute))
                        and (getattr(val, "id", None) == name or getattr(val, "attr", None) == name)):
                    continue
                if True:
                    parts = loc[2].split(".")
                    for j in range(1, len(parts) + 1):
                        out.add(f"{module}." + ".".join(parts[:j]))
        cache[name] = out
        return out

    def derived(self, path, names):
        """Module-level names computed from `names` at import time, transitively."""
        text = self.src(path)
        if not text:
            return set()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        found = set(names)
        changed = True
        while changed:
            changed = False
            for st in tree.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    used = {n.id for n in ast.walk(st) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                    if used & found:
                        new = bound_names(st) - found
                        if new:
                            found |= new
                            changed = True
        return found - set(names)

    # ---- per file
    def python_change(self, fp, old_text, notes):
        """Returns (mask, needs_import_check)."""
        new_text, removed, neighbours, added = apply_hunks(old_text, fp)
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            return None, False
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            notes.append("syntax error")
            return None, False
        if ast_dump(old_tree) == ast_dump(new_tree):
            notes.append("same ast")
            return 0, False

        path = fp.old
        mask = 0
        import_check = False

        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]

        def def_level(stmt, qual, tree_is_old):
            nonlocal mask, import_check
            if not self._constants_only(stmt, old_tree if not tree_is_old else new_tree):
                import_check = True
            names = bound_names(stmt)
            cls_key = f"{module}.{qual}" if qual else None
            cls_mask = self.classes.get(cls_key) if cls_key else None
            if isinstance(stmt, ast.ClassDef):
                # class header (bases, decorators, metaclass)
                key = f"{module}.{qual}.{stmt.name}" if qual else f"{module}.{stmt.name}"
                mask |= self.classes.get(key, 0) | self.name_users(stmt.name)
                mask |= self.range_mask(path, stmt.lineno, stmt.end_lineno)
                notes.append(f"class header {stmt.name}")
                return
            if isinstance(stmt, FUNCS) and tree_is_old:
                mask |= self.range_mask(path, stmt.lineno, stmt.end_lineno)
            if cls_mask is not None:
                # A statement in a class body: it reaches tests through
                # instances of the class (including anything the metaclass
                # derives from it), through code naming the class, or through
                # code that walks every subclass.
                short = qual.rsplit(".", 1)[-1]
                # nested classes (Snowflake.Tokenizer) are named with their parent
                ref = ".".join(qual.split(".")[-2:]) if "." in qual else short
                via_name = 0
                for nm in names:
                    via_name |= self.name_users(nm)
                reach = self.name_users(ref) | self.walkers()
                # classes that depend on this one at import time: the classes
                # it is nested in (a dialect reads its Tokenizer's settings when
                # it is created), classes that name it in their body
                # (Generator = TableauGenerator), and classes copying its tables
                users = {cls_key}
                parts = qual.split(".")
                for i in range(1, len(parts)):
                    users.add(f"{module}." + ".".join(parts[:i]))
                if "." not in qual:
                    users |= self.def_refs(short)
                users |= self.copiers(short, names)
                for k in users:
                    mask |= self.classes.get(k, 0)
                mask |= cls_mask | (via_name & reach)
                notes.append(f"class-level {short}.{sorted(names)[:2]}")
                return
            for nm in names | self.derived(path, names):
                mask |= self.name_users(nm)
            notes.append(f"def {sorted(names)[:3]}")
            if not names:
                mask |= self.file_mask(path)

        # removed lines, located in the old tree
        for ln in sorted(removed):
            loc = locate(old_tree, ln)
            if loc is None:
                continue
            kind, stmt, qual = loc
            if kind == "body":
                first = stmt.lineno
                last = stmt.end_lineno
                if hasattr(stmt, "body") and isinstance(stmt.body, list) and stmt.body and not isinstance(stmt, FUNCS):
                    last = stmt.body[0].lineno - 1
                mask |= self.range_mask(path, first, max(first, last)) | self.lines_mask(path, [ln])
            elif kind == "lambda":
                mask |= self.lines_mask(path, [ln])
                if not self._only_lambdas_changed(stmt, new_tree):
                    def_level(stmt, qual, True)
            else:
                def_level(stmt, qual, True)

        # added lines, located in the new tree
        for ln in sorted(added):
            loc = locate(new_tree, ln)
            if loc is None:
                continue
            kind, stmt, qual = loc
            if kind == "body":
                m = self.lines_mask(path, neighbours)
                if not m:
                    # nothing ran next to the insertion: take the whole enclosing function
                    fn = self._old_function(old_tree, qual, stmt)
                    if fn is not None:
                        m = self.range_mask(path, fn.lineno, fn.end_lineno)
                mask |= m
            elif kind == "lambda":
                mask |= self.lines_mask(path, neighbours)
                if not self._only_lambdas_changed(stmt, old_tree):
                    def_level(stmt, qual, False)
            else:
                def_level(stmt, qual, False)
        return mask, import_check

    def _old_function(self, old_tree, qual, stmt):
        target = qual.split(".") if qual else []
        if isinstance(stmt, FUNCS):
            target = target + [stmt.name]
        node = old_tree
        for part in target:
            nxt = None
            for s in getattr(node, "body", []):
                if isinstance(s, FUNCS + (ast.ClassDef,)) and s.name == part:
                    nxt = s
            if nxt is None:
                return node if isinstance(node, FUNCS) else None
            node = nxt
        return node if isinstance(node, FUNCS) else None

    def _constants_only(self, stmt, other_tree):
        """True when the statement's counterpart differs only in literal values
        inside plain containers (a table entry changing), which cannot raise
        at import time."""
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return False

        class Mask(ast.NodeTransformer):
            def visit_Constant(self, n):
                return ast.Constant(value=None)

        try:
            key = ast_dump(Mask().visit(ast.parse(ast.unparse(stmt))))
        except Exception:  # noqa: BLE001
            return False
        safe_parents = (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Assign, ast.AnnAssign,
                        ast.Module, ast.UnaryOp)
        for n in getattr(other_tree, "body", []):
            for cand in ast.walk(n):
                if type(cand) is not type(stmt) or getattr(cand, "lineno", None) is None:
                    continue
                try:
                    if ast_dump(Mask().visit(ast.parse(ast.unparse(cand)))) != key:
                        continue
                except Exception:  # noqa: BLE001
                    continue
                # same shape: check every constant sits directly in a container
                parents = {}
                for p in ast.walk(stmt):
                    for c in ast.iter_child_nodes(p):
                        parents[c] = p
                return all(isinstance(parents.get(c), safe_parents)
                           for c in ast.walk(stmt) if isinstance(c, ast.Constant))
        return False

    def _only_lambdas_changed(self, stmt, other_tree):
        try:
            key = strip_lambdas(stmt)
        except Exception:  # noqa: BLE001
            return False
        for n in ast.walk(other_tree):
            if isinstance(n, ast.stmt) and type(n) is type(stmt) and getattr(n, "lineno", None):
                try:
                    if strip_lambdas(n) == key:
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def import_fails(self, fps, new_texts):
        """Import every changed sqlglot module with the patch applied."""
        mods = []
        for fp in fps:
            p = fp.new or fp.old
            if p.startswith("sqlglot/") and p.endswith(".py"):
                mod = p[:-3].replace("/", ".")
                if mod.endswith(".__init__"):
                    mod = mod[: -len(".__init__")]
                mods.append(mod)
        if not mods:
            return False
        tmp = tempfile.mkdtemp(prefix="br-", dir="/tmp")
        try:
            shutil.copytree(SRC / "sqlglot", Path(tmp) / "sqlglot",
                            ignore=shutil.ignore_patterns("__pycache__"))
            for path, text in new_texts.items():
                if path.startswith("sqlglot/"):
                    dst = Path(tmp) / path
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(text, encoding="utf-8")
            code = "import importlib,sys\nimport sqlglot\nfor m in sys.argv[1:]: importlib.import_module(m)\n"
            env = dict(os.environ, PYTHONPATH=tmp, PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, "-c", code, *mods], env=env, cwd=tmp,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            if r.returncode != 0:
                log("import check failed:", r.stderr.decode(errors="replace").strip().splitlines()[-1:])
            return r.returncode != 0
        except Exception as e:  # noqa: BLE001
            log("import check error", e)
            return True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def select(self, patch_text):
        notes = []
        try:
            fps = parse_patch(patch_text)
        except Exception as e:  # noqa: BLE001
            return self.all_numbers, [f"unparsable patch: {e}"]
        if not fps:
            return set(), ["empty patch"]
        mask = 0
        everything = False
        check_imports = []
        new_texts = {}
        for fp in fps:
            path = fp.path
            base = path.rsplit("/", 1)[-1]
            if path == "<unknown>" or base in CONFIG_NAMES or fp.old != fp.new and fp.old and fp.new:
                notes.append(f"all: {path}")
                everything = True
                continue
            if fp.old is None:
                # a new file: nothing imports or opens it unless another change does
                notes.append(f"added {path}")
                continue
            if fp.new is None:
                notes.append(f"deleted {path}")
                everything = True
                continue
            old_text = self.src(path)
            if old_text is None:
                notes.append(f"unknown file {path}")
                everything = True
                continue
            mask |= self.opens.get(path, 0)
            if path.endswith(".py"):
                m, chk = self.python_change(fp, old_text, notes)
                if m is None:
                    everything = True
                    continue
                mask |= m
                if chk:
                    check_imports.append(fp)
                    new_texts[path] = apply_hunks(old_text, fp)[0]
                    if path.startswith("tests/") and path in self.byfile:
                        mask |= self.byfile[path]
            else:
                notes.append(f"data {path}")
                if path.startswith("tests/") and not self.opens.get(path):
                    # a data file no test opened at runtime: maybe read at import
                    stem = base.rsplit(".", 1)[0]
                    for tf, tm in self.byfile.items():
                        t = self.src(tf) or ""
                        if stem in t:
                            mask |= tm
        if everything:
            return self.all_numbers, notes
        if check_imports and self.import_fails(check_imports, new_texts):
            notes.append("import fails")
            return self.all_numbers, notes
        chosen = self.mask_to_numbers(mask)
        if chosen:
            chosen |= self.unmapped
        return chosen, notes


# --------------------------------------------------------------------------
# splitting

def split(numbers, ms):
    """Pick the worker count and assignment with the lowest cost."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for w in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * w
        workers = [[] for _ in range(w)]
        for n in order:
            j = loads.index(min(loads))
            loads[j] += ms[n]
            workers[j].append(n)
        cost = max(loads) + WORKER_MS * w
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    try:
        sel = Selector(tests)
    except Exception as e:  # noqa: BLE001
        log("map failed to load, running everything:", e)
        sel = None
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.stem
        try:
            if sel is None:
                raise RuntimeError("no map")
            chosen, notes = sel.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            chosen, notes = {t["number"] for t in tests}, [f"error {e!r}"]
        workers = split(chosen, ms)
        log(f"{commit}: {len(chosen)} tests on {len(workers)} workers {notes}")
        sys.stdout.write(json.dumps({"commit": commit, "workers": workers}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
