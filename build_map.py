"""Build-time: merge coverage dumps into one compact map.

Output (marshal):
  tests   list of test ids; a test's bit in every mask is its index here
  lines   {file: {line: mask}}  tests that ran this line (outside module/class bodies)
  opens   {file: mask}          tests that opened this file as data
  byfile  {test file: mask}     tests defined in each test file
"""
import glob
import json
import marshal
import sys
from collections import defaultdict


def import_graph(root):
    """repo .py file -> repo .py files it imports (directly), for sqlglot/ and tests/."""
    import ast
    import os
    graph = {}

    def resolve(mod):
        base = mod.replace(".", "/")
        for cand in (base + ".py", base + "/__init__.py"):
            if os.path.isfile(os.path.join(root, cand)):
                return cand
        return None

    for top in ("sqlglot", "tests"):
        for dp, _, fs in os.walk(os.path.join(root, top)):
            for f in fs:
                if not f.endswith(".py"):
                    continue
                path = os.path.relpath(os.path.join(dp, f), root)
                try:
                    tree = ast.parse(open(os.path.join(root, path), "rb").read())
                except SyntaxError:
                    continue
                pkg = path[:-3].replace("/", ".")
                pkg_parts = pkg.split(".")
                if f != "__init__.py":
                    pkg_parts = pkg_parts[:-1]
                else:
                    pkg_parts = pkg_parts[:-1]
                deps = set()
                for n in ast.walk(tree):
                    mods = []
                    if isinstance(n, ast.ImportFrom):
                        if n.level:
                            base_parts = pkg_parts[: len(pkg_parts) - (n.level - 1)] if n.level > 1 else pkg_parts
                            base = ".".join(base_parts + ([n.module] if n.module else []))
                        else:
                            base = n.module or ""
                        mods = [base] + [f"{base}.{a.name}" for a in n.names]
                    elif isinstance(n, ast.Import):
                        mods = [a.name for a in n.names]
                        # "import a.b.c" also runs a and a.b
                        for a in n.names:
                            parts = a.name.split(".")
                            mods += [".".join(parts[:i]) for i in range(1, len(parts))]
                    for m in mods:
                        r = resolve(m) if m else None
                        if r and r != path:
                            deps.add(r)
                        # importing a.b.c also runs package __init__ files
                        if m:
                            parts = m.split(".")
                            for i in range(1, len(parts)):
                                r2 = resolve(".".join(parts[:i]))
                                if r2 and r2 != path:
                                    deps.add(r2)
                graph[path] = deps
    return graph


def class_indexes(root):
    """Import-time wiring between classes, from the pinned source:
      aliases: name -> class keys whose body assigns that name plainly
               (Generator = TableauGenerator), with the classes they sit in
      chains:  attr -> [(dotted chain, class key or "")] for class/module
               bodies reading Some.Class.attr at import time"""
    import ast
    import os
    aliases, chains = defaultdict(set), defaultdict(set)
    for dp, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if not f.endswith(".py"):
                continue
            path = os.path.relpath(os.path.join(dp, f), root)
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            try:
                tree = ast.parse(open(os.path.join(root, path), "rb").read())
            except SyntaxError:
                continue

            def visit(stmts, qual):
                for st in stmts:
                    if isinstance(st, ast.ClassDef):
                        visit(st.body, qual + [st.name])
                        continue
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    keys = [f"{module}." + ".".join(qual[:j]) for j in range(1, len(qual) + 1)]
                    if isinstance(st, (ast.Assign, ast.AnnAssign)) and qual:
                        v = st.value
                        if isinstance(v, ast.Name):
                            aliases[v.id].update(keys)
                        elif isinstance(v, ast.Attribute):
                            aliases[v.attr].update(keys)
                    for n in ast.walk(st):
                        if isinstance(n, ast.Lambda):
                            continue
                        if isinstance(n, ast.Attribute):
                            parts = []
                            x = n
                            while isinstance(x, ast.Attribute):
                                parts.append(x.attr)
                                x = x.value
                            if isinstance(x, ast.Name):
                                parts.append(x.id)
                                chain = ".".join(reversed(parts))
                                chains[n.attr].add((chain, f"{module}." + ".".join(qual) if qual else ""))

            visit(tree.body, [])
    return ({k: sorted(v) for k, v in aliases.items()},
            {k: sorted(v) for k, v in chains.items()})


def eager_modules(root):
    import subprocess
    code = ("import sys, sqlglot, os\n"
            "r = os.path.realpath(sys.argv[1])\n"
            "for m in list(sys.modules.values()):\n"
            "    f = getattr(m, '__file__', None) or ''\n"
            "    f = os.path.realpath(f)\n"
            "    if f.startswith(r): print(os.path.relpath(f, r))\n")
    out = set()
    for attempt in range(3):
        r = subprocess.run([sys.executable, "-c", code, root], capture_output=True, text=True,
                           env={**__import__("os").environ, "PYTHONPATH": root}, cwd="/")
        out = set(r.stdout.split())
        if out:
            return out
        print(f"eager module scan failed (rc={r.returncode}): {r.stderr[-500:]}", file=sys.stderr)
    print("cannot list the modules `import sqlglot` loads; treating every import as global", file=sys.stderr)
    return None


# Expensive tests we can check piece by piece inside the container
# (see verify.py). For these, keep which lines each piece ran.
PARTED = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
}


def main(cov_dir, out_path, root, groups=None):
    eager = eager_modules(root)
    aliases_idx, chains_idx = class_indexes(root)
    paths = glob.glob(f"{cov_dir}/cov-*.json")
    broken = []
    if groups is not None and len(paths) < groups:
        broken.append(f"expected {groups} coverage files, found {len(paths)}")
    collected = 0
    items = set()
    hits = defaultdict(lambda: defaultdict(set))   # key -> file -> lines
    opens = defaultdict(set)                       # key -> files
    inst = defaultdict(set)                        # key -> class names
    probes = {}                                    # (file, line, col) -> [runs, is_eq_differ, ever_equal]
    for path in paths:
        with open(path) as fh:
            d = json.load(fh)
        collected += d.get("collected", 0)
        items.update(d.get("items", ()))
        for k, files in d["hits"].items():
            for f, ls in files.items():
                hits[k][f].update(ls)
        for k, fs in d["opens"].items():
            opens[k].update(fs)
        for k, cs in d.get("inst", {}).items():
            inst[k].update(cs)
        for f, ln, col, runs, differ, equal in d.get("probes", []):
            r = probes.setdefault((f, ln, col), [0, 0, 0])
            r[0] += runs
            r[1] |= differ
            r[2] |= equal

    tests = sorted(k[len("call::"):] for k in hits.keys() | opens.keys() if k.startswith("call::"))
    # tests whose call phase hit nothing still exist; take ids from setup too
    tests = sorted(set(tests) | {k[len("setup::"):] for k in hits.keys() | opens.keys() if k.startswith("setup::")}
                   | items)   # tests skipped before running any code still exist
    index = {t: i for i, t in enumerate(tests)}

    def group(t):          # class (or module) a test belongs to
        return t.rsplit("::", 1)[0]

    def tfile(t):
        return t.split("::", 1)[0]

    byfile = defaultdict(int)
    bygroup = defaultdict(int)
    for t, i in index.items():
        byfile[tfile(t)] |= 1 << i
        bygroup[group(t)] |= 1 << i

    graph = import_graph(root)
    closure = {}

    def deps(f, seen=None):
        if f in closure:
            return closure[f]
        seen = seen or set()
        out = {f}
        for d in graph.get(f, ()):
            if d not in seen:
                seen.add(d)
                out |= deps(d, seen)
        closure[f] = out
        return out

    importers = defaultdict(set)      # repo file -> repo files importing it (transitively), itself included
    for tf in graph:
        for d in deps(tf):
            importers[d].add(tf)
    deferred = []

    lines = defaultdict(lambda: defaultdict(int))
    fopens = defaultdict(int)
    classes = defaultdict(int)

    def add(mask, files_lines, files_opened, made=()):
        for c in made:
            classes[c] |= mask
        for f, ls in files_lines.items():
            d = lines[f]
            for ln in ls:
                d[ln] |= mask
        for f in files_opened:
            fopens[f] |= mask

    # lines that run while modules are being imported (by sqlglot or by test
    # modules); a change there cannot be checked by patching a live process
    importtime = defaultdict(set)
    for k, files in hits.items():
        if k.split("::", 1)[0] in ("import", "collect", "boot"):
            for f, ls in files.items():
                importtime[f].update(ls)

    parted_bits = {index[t]: t for t in PARTED if t in index}
    nonpart = {t: defaultdict(set) for t in parted_bits.values()}
    parts = {t: defaultdict(lambda: defaultdict(set)) for t in parted_bits.values()}

    def note_nonpart(mask, files_lines):
        for bit, t in parted_bits.items():
            if mask >> bit & 1:
                for f, ls in files_lines.items():
                    nonpart[t][f].update(ls)

    for k in hits.keys() | opens.keys() | inst.keys():
        phase, _, name = k.partition("::")
        if phase in ("sub", "work"):
            test, _, label = name.partition("##")
            if test not in index:
                continue
            add(1 << index[test], hits.get(k, {}), opens.get(k, ()), inst.get(k, ()))
            if test in parts:
                for f, ls in hits.get(k, {}).items():
                    parts[test][f"{phase}:{label}"][f].update(ls)
            continue
        if phase == "call" and name in index:
            mask = 1 << index[name]
        elif phase in ("setup", "teardown") and name in index:
            # class setup/teardown runs inside the first/last test's phases,
            # but every test in the class depends on it
            mask = bygroup[group(name)]
        elif phase == "collect":
            mask = 0
            for tf in importers.get(name, {name}):
                mask |= byfile.get(tf, 0)
        elif phase == "import":
            deferred.append(k)
            continue
        else:
            continue
        if mask:
            add(mask, hits.get(k, {}), opens.get(k, ()), inst.get(k, ()))
            note_nonpart(mask, hits.get(k, {}))

    if collected and len(tests) < collected:
        broken.append(f"collected {collected} tests, mapped {len(tests)}")
    if len(tests) < 1000 or len(lines) < 100:
        broken.append(f"only {len(tests)} tests, {len(lines)} files")
    if broken:
        # never fail the build: a broken map makes the selector run everything
        print("MAP BROKEN, selector will run every test:", broken, file=sys.stderr)
    # Code that ran while a sqlglot module was being imported (metaclasses,
    # module-level calls) matters to every test that loads that module.
    everyone = (1 << len(tests)) - 1
    def module_users(f):
        """Tests that use module file f at run time: ran its lines or made its classes."""
        m = 0
        for v in lines.get(f, {}).values():
            m |= v
        modname = f[:-3].replace("/", ".")
        if modname.endswith(".__init__"):
            modname = modname[: -len(".__init__")]
        for c, v in classes.items():
            if c.startswith(modname + "."):
                m |= v
        return m

    import_mask = {}

    def mask_for_import(mod_file):
        if mod_file in import_mask:
            return import_mask[mod_file]
        if eager is None or mod_file in eager:
            m = everyone
        else:
            m = 0
            for x in importers.get(mod_file, {mod_file}):
                m |= byfile.get(x, 0) | module_users(x)
            if not m:
                m = everyone
        import_mask[mod_file] = m
        return m

    for k in deferred:
        mask = mask_for_import(k.split("::", 1)[1])
        add(mask, hits.get(k, {}), opens.get(k, ()), inst.get(k, ()))
        note_nonpart(mask, hits.get(k, {}))
    print(f"eager modules: {len(eager)}, import contexts: {len(deferred)}", file=sys.stderr)

    # many lines share a mask: store each mask once and point at it
    table, ids = [], {}

    def ref(mask):
        i = ids.get(mask)
        if i is None:
            i = ids[mask] = len(table)
            table.append(mask)
        return i

    out = {
        "tests": tests,
        "masks": None,
        # each file's line table is marshalled separately so the selector
        # only decodes the files a change touches
        "lines": {f: marshal.dumps({ln: ref(v) for ln, v in d.items()}) for f, d in lines.items()},
        "opens": dict(fopens),
        "byfile": dict(byfile),
        "classes": dict(classes),
        "importtime": {f: sorted(ls) for f, ls in importtime.items()},
        "aliases": aliases_idx,
        "chains": chains_idx,
        "probes": {f"{f}:{ln}:{col}": (1 if r[0] else 0) | (r[1] << 1) | (r[2] << 2) for (f, ln, col), r in probes.items()},

    }
    out["masks"] = table
    out["broken"] = broken
    with open(out_path, "wb") as fh:
        marshal.dump(out, fh)
    print(f"map: {len(tests)} tests, {len(out['lines'])} files, {len(fopens)} opened files, {len(classes)} classes, {len(probes)} comparison sites", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else None)
