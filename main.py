#!/usr/bin/env python
from __future__ import annotations

import ast
import collections
import dataclasses
import functools
import itertools
import json
import pathlib
import typing
import warnings

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


def find_sccs(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    """
    Kosaraju's algorithm: returns SCCs in reverse topological order.
    """
    visited = set()
    finish_order = []

    def dfs_forward(node: str) -> None:
        stack = [(node, iter(graph.get(node, ())))]
        while stack:
            n, children = stack[-1]
            try:
                child = next(children)
                if child not in visited:
                    visited.add(child)
                    stack.append((child, iter(graph.get(child, ()))))
            except StopIteration:
                finish_order.append(n)
                stack.pop()

    for node in graph:
        if node in visited:
            continue
        visited.add(node)
        dfs_forward(node)

    reverse = collections.defaultdict(set)
    for node, deps in graph.items():
        for dep in deps:
            reverse[dep].add(node)

    visited.clear()
    sccs = []

    def dfs_reverse(start: str) -> frozenset[str]:
        component: set[str] = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            component.add(n)
            stack.extend(reverse.get(n, ()))
        return frozenset(component)

    for node in reversed(finish_order):
        if node not in visited:
            sccs.append(dfs_reverse(node))

    return sccs


def collapse_sccs(
    raw: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    sccs = find_sccs(raw)
    canonical = {}
    for scc in sccs:
        canon = min(scc)
        for name in scc:
            canonical[name] = canon

    condensed = collections.defaultdict(set)
    for node, deps in raw.items():
        c = canonical[node]
        for dep in deps:
            cd = canonical[dep]
            if cd != c:
                condensed[c].add(cd)

    return dict(condensed), canonical


def main():
    try:
        deps_map = json.loads(CACHE_FILE.read_text())
    except:
        deps_map = build_dep_tree()
        CACHE_FILE.write_text(json.dumps(deps_map, indent=4, sort_keys=True) + "\n")

    raw_deps_map = {name: set(deps) for name, deps in deps_map.items()}

    # Now we deal with the circular imports :/
    deps_map, canonical = collapse_sccs(raw_deps_map)

    @functools.cache
    def tree_of(name: str) -> dict[str, dict]:
        canon = canonical.get(name, name)
        return {dep: tree_of(dep) for dep in deps_map.get(canon, ())}

    @functools.cache
    def is_depof(x: str, y: str) -> bool:
        """
        is `x` a sub-dependency of `y`?

        Examples
        --------
        >>> is_depof("ast", "_ast")
        False
        >>> is_depof("_ast", "ast")
        True
        """
        cx = canonical.get(x, x)
        cy = canonical.get(y, y)
        if cx == cy:
            return False
        cy_tree = tree_of(cy)
        return (cx in cy_tree) or any(is_depof(cx, dep) for dep in cy_tree)

    all_libs = frozenset(raw_deps_map)
    stats = collections.defaultdict(int)
    for x, y in itertools.permutations(all_libs, 2):
        stats[x] += int(is_depof(x, y))

    print(collections.Counter(stats).most_common(99))

    dumped = json.dumps(stats, indent=4, sort_keys=True)

    (ROOT / "stats.json").write_text(dumped + "\n")


if __name__ == "__main__":
    main()
