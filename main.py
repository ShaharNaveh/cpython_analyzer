#!/usr/bin/env python
from __future__ import annotations

import ast
import collections
import dataclasses
import functools
import graphlib
import itertools
import json
import pathlib
import typing
import warnings

# import sys
# sys.setrecursionlimit(15000)

if typing.TYPE_CHECKING:
    from collections.abc import Iterable


ROOT = pathlib.Path(__file__).parent
CACHE_FILE = ROOT / ".tree.json"
LIB_PATH = ROOT / "cpython/Lib"
EXCLUDES = {LIB_PATH / "test", LIB_PATH / "idlelib/idle_test"}


class RewriteIfDunderMain(ast.NodeTransformer):
    @staticmethod
    def _is_main_block(node: ast.If) -> bool:

        test = node.test

        if not isinstance(test, ast.Compare):
            return False

        if (len(test.ops) != 1) or (not isinstance(test.ops[0], ast.Eq)):
            return False

        left = test.left
        comparator = test.comparators[0]
        is_name = isinstance(left, ast.Name) and left.id == "__name__"

        return is_name and (
            isinstance(comparator, ast.Constant) and comparator.value == "__main__"
        )

    def visit_If(self, node: ast.If):
        if self._is_main_block(node):
            return None

        return self.generic_visit(node)


class ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports = set()

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            # `import os.path` module is `os`
            name = alias.name.split(".")[0]
            self.imports.add(name)

        return self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0:  # We don't care about relative imports
            return self.generic_visit(node)

        module = getattr(node, "module", None)
        if module is None:  # Ignore `from . import my_internal_module`
            return self.generic_visit(node)

        # `from foo.bar import ...` module is `foo`
        name = module.split(".")[0]
        self.imports.add(name)

        return self.generic_visit(node)


@dataclasses.dataclass(frozen=True, slots=True)
class LibEntry:
    name: str
    deps: frozenset[str]

    @classmethod
    def from_path(cls, path: pathlib.Path) -> typing.Self:
        # `Lib/pathlib/x.py` lib name is `pathlib` not `x`.
        name = next(
            part for part in path.relative_to(LIB_PATH).parts if part != "test"
        ).removesuffix(".py")

        contents = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(contents)
        except SyntaxError:
            warnings.warn(f"Could not parse {path}")
            tree = ast.Module()

        tree = RewriteIfDunderMain().visit(tree)
        ast.fix_missing_locations(tree)

        visitor = ImportVisitor()
        visitor.visit(tree)
        deps = frozenset(visitor.imports - {name})

        return cls(name, deps)


def iter_lib(root: pathlib.Path = LIB_PATH) -> Iterable[pathlib.Path]:
    for child in root.iterdir():
        if child in EXCLUDES:
            continue

        is_dir = not child.is_file()

        if (not is_dir) and child.suffix != ".py":
            continue

        if is_dir:
            yield from iter_lib(child)
        else:
            yield child


def build_dep_tree():
    deps_map = collections.defaultdict(set)
    libs_it = itertools.chain(iter_lib(), iter_lib(LIB_PATH / "test/libregrtest"))
    for lib_entry in map(LibEntry.from_path, libs_it):
        name, deps = dataclasses.astuple(lib_entry)
        deps_map[name] |= deps

    return {name: list(deps) for name, deps in deps_map.items()}


def main():
    try:
        deps_map = json.loads(CACHE_FILE.read_text())
    except:
        deps_map = build_dep_tree()
        CACHE_FILE.write_text(json.dumps(deps_map, indent=4, sort_keys=True) + "\n")

    raw_deps_map = {name: set(deps) for name, deps in deps_map.items()}

    deps_map = raw_deps_map.copy()
    cycle_pairs = {}
    while True:
        ts = graphlib.TopologicalSorter(deps_map)
        try:
            tuple(ts.static_order())
            break
        except graphlib.CycleError as err:
            cycle = err.args[1]
            modules = list(set(cycle))[:2]
            a, b = modules
            cycle_pairs[a] = b
            cycle_pairs[b] = a
            # join the deps of both
            deps_map[a] |= deps_map.pop(b) - {a}

    """
    @functools.cache
    def tree_of(name: str) -> dict[str, dict]:
        seen = set()

        def inner(s: str):
            seen.add(s)
            return {dep: inner(dep) for dep in (deps_map.get(s, frozenset()) - seen)}

        return inner(name)
    """

    @functools.cache
    def tree_of(name: str, *, depth: int = 1) -> dict[str, dict]:
        if depth == 0:
            return {}

        ndepth = depth - 1
        out = {
            dep: tree_of(dep, depth=ndepth) for dep in deps_map.get(name, frozenset())
        }

        if pair := cycle_pairs.get(name):
            return tree_of(pair, depth=ndepth) | out

        return out

    """
    ts = graphlib.TopologicalSorter(deps_map)
    for lib in reversed(tuple(ts.static_order())):
        tree_of(lib)  # Prime the caches in order to not get inf recursion
    """
    x = tree_of("pathlib")
    print(json.dumps(x, indent=4))


if __name__ == "__main__":
    main()
