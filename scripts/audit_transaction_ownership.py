"""List AST-derived transaction and connection ownership candidates.

The report preserves lexical source evidence. It does not resolve reachability,
dynamic dispatch, or runtime object identity, and never proves complete writer
coverage or atomicity. Python sources are parsed but never imported or executed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

_CONTEXT_MARKERS = {
    "read_connection",
    "locked_read",
    "write_connection",
    "write_cursor",
    "transaction",
    "locked_transaction",
}
_TRANSACTION_MARKERS = {"commit", "rollback"}
_SQL_SINKS = {"execute", "executemany", "executescript"}


def _attribute_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _import_kind(module: str, name: str = "") -> str | None:
    normalized = module.lower()
    if normalized == "sqlite3" or normalized.startswith("sqlite3."):
        return "sqlite_connect" if name == "connect" else "sqlite_module"
    if normalized in {"psycopg", "psycopg2"} or normalized.startswith(("psycopg.", "psycopg2.")):
        return "external_connect" if name == "connect" else "external_module"
    return None


class _OwnershipCandidates(ast.NodeVisitor):
    def __init__(self, path: str, source_scope: str) -> None:
        self.path = path
        self.source_scope = source_scope
        self.owners: list[str] = []
        self.lexical_scope: list[str] = []
        self.imports: dict[str, str] = {}
        self.hints: dict[str, str] = {}
        self.class_enclosing_states: list[tuple[dict[str, str], dict[str, str]]] = []
        self.class_owner_depths: list[int] = []
        self.candidates: list[dict[str, Any]] = []

    @property
    def owner(self) -> str:
        return ".".join(self.owners) if self.owners else "<module>"

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            name = item.asname or item.name.split(".")[0]
            self._invalidate_target(ast.Name(id=name, ctx=ast.Store()))
            kind = _import_kind(item.name)
            if kind:
                self.imports[name] = kind

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for item in node.names:
            name = item.asname or item.name
            self._invalidate_target(ast.Name(id=name, ctx=ast.Store()))
            kind = _import_kind(module, item.name)
            if kind:
                self.imports[name] = kind

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        enclosing = self._state()
        # Nested class bodies, like methods, cannot close over a class namespace.
        in_class = self.class_owner_depths and len(self.owners) == self.class_owner_depths[-1]
        visible = self.class_enclosing_states[-1] if in_class else enclosing
        self._set_state(visible)
        self.class_enclosing_states.append(visible)
        self.owners.append(node.name)
        self.class_owner_depths.append(len(self.owners))
        self._visit_statements(node.body)
        self.class_owner_depths.pop()
        self.owners.pop()
        self.class_enclosing_states.pop()
        self._set_state(enclosing)
        self._invalidate_target(ast.Name(id=node.name, ctx=ast.Store()))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.owners.append(node.name)
        old_imports = self.imports
        # A method does not close over its class body's namespace.
        if self.class_owner_depths and len(self.owners) == self.class_owner_depths[-1] + 1:
            self.imports = dict(self.class_enclosing_states[-1][0])
        visible_imports = self.imports
        local_names = {argument.arg for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
        if node.args.vararg is not None:
            local_names.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            local_names.add(node.args.kwarg.arg)
        # A local store shadows a module import throughout the function scope.
        # Including nested-scope stores is conservative: it can leave a hint
        # unknown, but cannot promote a rebound alias to a known connection.
        local_names.update(
            child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
        )
        for statement in node.body:
            for child in ast.walk(statement):
                local_names.update(self._binding_names(child))
        self.imports = {name: kind for name, kind in visible_imports.items() if name not in local_names}
        old_hints = self.hints
        self.hints = {}
        self.generic_visit(node)
        self.hints = old_hints
        self.imports = old_imports
        self.owners.pop()
        self._invalidate_target(ast.Name(id=node.name, ctx=ast.Store()))

    visit_AsyncFunctionDef = visit_FunctionDef

    def _state(self) -> tuple[dict[str, str], dict[str, str]]:
        return dict(self.imports), dict(self.hints)

    def _set_state(self, state: tuple[dict[str, str], dict[str, str]]) -> None:
        self.imports, self.hints = (dict(part) for part in state)

    def _merge_states(self, states: list[tuple[dict[str, str], dict[str, str]]]) -> None:
        merged = []
        for index in range(2):
            first = states[0][index]
            merged.append(
                {key: value for key, value in first.items() if all(s[index].get(key) == value for s in states[1:])}
            )
        self._set_state((merged[0], merged[1]))

    def _visit_statements(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self.visit(statement)

    @staticmethod
    def _binding_names(node: ast.AST) -> set[str]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return {node.name}
        if isinstance(node, ast.Import):
            return {item.asname or item.name.split(".")[0] for item in node.names}
        if isinstance(node, ast.ImportFrom):
            return {item.asname or item.name for item in node.names if item.name != "*"}
        return set()

    def _invalidate_stores(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            for child in ast.walk(statement):
                if isinstance(child, (ast.Name, ast.Attribute)) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    self._invalidate_target(child)
                for name in self._binding_names(child):
                    self._invalidate_target(ast.Name(id=name, ctx=ast.Store()))

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        before = self._state()
        self._visit_statements(node.body)
        body = self._state()
        self._set_state(before)
        self._visit_statements(node.orelse)
        self._merge_states([body, self._state()])

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        before = self._state()
        self.visit(node.body)
        body = self._state()
        self._set_state(before)
        self.visit(node.orelse)
        self._merge_states([body, self._state()])

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        states = []
        for value in node.values:
            self.visit(value)
            states.append(self._state())
        self._merge_states(states)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        before = self._state()
        self._invalidate_stores(node.body)
        self._invalidate_target(node.target)
        uncertain = self._state()
        self._visit_statements(node.body)
        body = self._state()
        self._merge_states([before, uncertain, body])
        self._visit_statements(node.orelse)
        self._merge_states([before, uncertain, body, self._state()])

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self._invalidate_stores(node.body)
        self.visit(node.test)
        before = self._state()
        self._visit_statements(node.body)
        body = self._state()
        self._merge_states([before, body])
        self._visit_statements(node.orelse)
        self._merge_states([before, body, self._state()])

    def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
        possible = [self._state()]
        for statement in node.body:
            self.visit(statement)
            possible.append(self._state())
        self._visit_statements(node.orelse)
        exits = [self._state()]
        for handler in node.handlers:
            self._merge_states(possible)
            self._invalidate_stores(node.body)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name:
                self.imports.pop(handler.name, None)
                self.hints.pop(handler.name, None)
            self._visit_statements(handler.body)
            if handler.name:
                self.imports.pop(handler.name, None)
                self.hints.pop(handler.name, None)
            exits.append(self._state())
        self._merge_states(exits + possible if node.finalbody else exits)
        self._visit_statements(node.finalbody)

    visit_TryStar = visit_Try

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        before = self._state()
        exits = [before]
        for case in node.cases:
            self._set_state(before)
            # Pattern captures are local assignments; do not infer their values.
            for child in ast.walk(case.pattern):
                name = getattr(child, "name", None)
                if name:
                    self.imports.pop(name, None)
                    self.hints.pop(name, None)
                if isinstance(child, ast.MatchMapping) and child.rest:
                    self.imports.pop(child.rest, None)
                    self.hints.pop(child.rest, None)
            if case.guard is not None:
                self.visit(case.guard)
            self._visit_statements(case.body)
            exits.append(self._state())
        self._merge_states(exits)

    def _call_kind(self, node: ast.Call) -> str | None:
        name = _attribute_name(node.func)
        if isinstance(node.func, ast.Name):
            imported = self.imports.get(node.func.id)
            if imported in {"sqlite_connect", "external_connect"}:
                return imported
        if name in _CONTEXT_MARKERS | _TRANSACTION_MARKERS | _SQL_SINKS | {"get_write_connection"}:
            return name
        if name == "connect":
            if isinstance(node.func, ast.Name):
                imported = self.imports.get(node.func.id)
                return imported if imported in {"sqlite_connect", "external_connect"} else None
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                module_kind = self.imports.get(node.func.value.id)
                if module_kind == "sqlite_module":
                    return "sqlite_connect"
                if module_kind == "external_module":
                    return "external_connect"
        return None

    @staticmethod
    def _receiver_key(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = _OwnershipCandidates._receiver_key(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    def _connection_hint(self, node: ast.AST | None) -> str:
        if node is None:
            return "unknown"
        key = self._receiver_key(node)
        if key and key in self.hints:
            return self.hints[key]
        if isinstance(node, ast.Call):
            called = _attribute_name(node.func)
            if called == "get_write_connection":
                return "shared_write_connection_candidate"
            if called in {"transaction", "locked_transaction"}:
                return "shared_transaction_candidate"
            if called == "sqlite_connect":
                return "private_sqlite_connection_candidate"
            if called == "connect" and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                module_kind = self.imports.get(node.func.value.id)
                if module_kind == "external_module":
                    return "external_connection_candidate"
                if module_kind == "sqlite_module":
                    return "private_sqlite_connection_candidate"
        if isinstance(node, ast.Attribute) and node.attr == "_conn":
            return "private_raw_connection_candidate"
        return "unknown"

    def _boundary(self, hint: str) -> str:
        if self.path.startswith(("scripts/fixtures/", "scripts/test_fixtures/")):
            return "diagnostic_fixture"
        if self.path.startswith("rag/data_sources/") or hint.startswith("external_"):
            return "external_datasource"
        if self.source_scope == "scripts":
            return "maintenance_or_unknown_script"
        if hint.startswith("private_"):
            return "private_store"
        if any(owner == "UserDB" for owner in self.owners):
            return "shared_userdb_raw"
        if hint.startswith("shared_"):
            return "shared_userdb_candidate"
        return "unknown"

    def _transaction_ownership(self, marker: str, hint: str) -> str:
        if self.path.startswith(("scripts/fixtures/", "scripts/test_fixtures/")):
            return "diagnostic_fixture_candidate"
        if self.source_scope == "scripts" and not any(item in _CONTEXT_MARKERS for item in self.lexical_scope):
            return "unknown"
        if hint.startswith("external_"):
            return "external_boundary"
        if hint.startswith("private_"):
            return "private_direct_commit" if marker == "commit" else "private_store_candidate"
        if hint.startswith("shared_") or any(item in _CONTEXT_MARKERS for item in self.lexical_scope):
            return "candidate_only"
        return "unknown"

    def _emit(self, node: ast.Call, marker: str, receiver: ast.AST | None) -> None:
        if marker == "sqlite_connect":
            hint = "private_sqlite_connection_candidate"
        elif marker == "external_connect":
            hint = "external_connection_candidate"
        elif marker == "get_write_connection":
            hint = "shared_write_connection_candidate"
        elif marker in {"transaction", "locked_transaction"}:
            hint = "shared_transaction_candidate"
        elif marker in {"write_connection", "write_cursor"}:
            hint = "shared_write_context_candidate"
        elif marker in {"read_connection", "locked_read"}:
            hint = "shared_read_context_candidate"
        else:
            hint = self._connection_hint(receiver)
        self.candidates.append(
            {
                "path": self.path,
                "line": node.lineno,
                "column": node.col_offset + 1,
                "owner": self.owner,
                "marker": marker,
                "lexical_scope": list(self.lexical_scope),
                "connection_hint": hint,
                "boundary": self._boundary(hint),
                "transaction_ownership": self._transaction_ownership(marker, hint),
            }
        )

    def visit_With(self, node: ast.With) -> None:
        before = self._state()
        added_scopes: list[str] = []
        for item in node.items:
            context = item.context_expr
            name = _attribute_name(context.func) if isinstance(context, ast.Call) else _attribute_name(context)
            is_context = name in _CONTEXT_MARKERS
            if is_context:
                self.lexical_scope.append(name)
                added_scopes.append(name)
            self.visit(context)
            self._invalidate_target(item.optional_vars)
            if is_context and item.optional_vars is not None:
                key = self._receiver_key(item.optional_vars)
                if key:
                    self.hints[key] = (
                        "shared_transaction_candidate"
                        if name in {"transaction", "locked_transaction"}
                        else "shared_connection_candidate"
                    )
        for statement in node.body:
            self.visit(statement)
        after = self._state()
        # Exceptions may be suppressed at any point in the body. Preserve
        # sequential hints while visiting sinks, then invalidate any binding
        # that could differ at such an exit before merging the final state.
        self._invalidate_stores(node.body)
        uncertain = self._state()
        self._merge_states([before, uncertain, after])
        if added_scopes:
            del self.lexical_scope[-len(added_scopes) :]

    visit_AsyncWith = visit_With

    def _assigned_hint(self, value: ast.AST | None) -> str:
        if isinstance(value, (ast.Name, ast.Attribute)):
            return self._connection_hint(value)
        if not isinstance(value, ast.Call):
            return "unknown"
        kind = self._call_kind(value)
        if kind == "sqlite_connect":
            return "private_sqlite_connection_candidate"
        if kind == "external_connect":
            return "external_connection_candidate"
        if kind == "get_write_connection":
            return "shared_write_connection_candidate"
        if kind in {"transaction", "locked_transaction"}:
            return "shared_transaction_candidate"
        return "unknown"

    def _invalidate_target(self, target: ast.AST | None) -> None:
        if target is None:
            return
        for child in ast.walk(target):
            if isinstance(child, (ast.Name, ast.Attribute)):
                key = self._receiver_key(child)
                if key:
                    for existing in list(self.hints):
                        if existing == key or existing.startswith(key + "."):
                            self.hints.pop(existing, None)
                if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    self.imports.pop(child.id, None)

    def _record_assignment(self, target: ast.AST, value: ast.AST | None) -> None:
        key = self._receiver_key(target)
        hint = self._assigned_hint(value)
        self._invalidate_target(target)
        if key:
            if hint == "unknown":
                self.hints.pop(key, None)
            else:
                self.hints[key] = hint
        if isinstance(target, ast.Attribute) and target.attr == "_conn":
            self._emit_attribute_candidate(target)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Visit the expression itself so a constructor Call reaches visit_Call;
        # generic_visit(value) would only walk its children and miss its marker.
        self.visit(node.value)
        for target in node.targets:
            self._record_assignment(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.annotation)
        self._record_assignment(node.target, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._record_assignment(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._invalidate_target(node.target)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._invalidate_target(target)

    def _emit_attribute_candidate(self, node: ast.Attribute) -> None:
        hint = self._connection_hint(node)
        boundary = self._boundary(hint)
        if any(owner == "UserDB" for owner in self.owners):
            boundary = "shared_userdb_raw"
        self.candidates.append(
            {
                "path": self.path,
                "line": node.lineno,
                "column": node.col_offset + 1,
                "owner": self.owner,
                "marker": "raw_connection_access",
                "lexical_scope": list(self.lexical_scope),
                "connection_hint": hint,
                "boundary": boundary,
                "transaction_ownership": "unknown",
            }
        )

    def visit_Call(self, node: ast.Call) -> None:
        marker = self._call_kind(node)
        if marker in _CONTEXT_MARKERS | _TRANSACTION_MARKERS | _SQL_SINKS | {
            "get_write_connection",
            "sqlite_connect",
            "external_connect",
        }:
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            if marker in {"sqlite_connect", "external_connect"}:
                receiver = node.func if isinstance(node.func, ast.Name) else node.func.value
            self._emit(node, marker, receiver)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "_conn":
            self._emit_attribute_candidate(node)
        self.generic_visit(node)


def _source_files(root: Path, scope: str, errors: list[dict[str, Any]]) -> list[Path]:
    files: list[Path] = []

    def on_error(exc: OSError) -> None:
        errors.append({"path": f"{scope}/.", "kind": "directory_read_error", "error_type": type(exc).__name__})

    for directory, names, filenames in os.walk(root, followlinks=False, onerror=on_error):
        current = Path(directory)
        kept: list[str] = []
        for name in names:
            child = current / name
            if child.is_symlink():
                errors.append(
                    {"path": f"{scope}/{child.relative_to(root).as_posix()}", "kind": "symlink_directory_skipped"}
                )
            else:
                kept.append(name)
        names[:] = kept
        for filename in filenames:
            path = current / filename
            if path.suffix.lower() != ".py":
                continue
            if path.is_symlink():
                errors.append({"path": f"{scope}/{path.relative_to(root).as_posix()}", "kind": "symlink_file_skipped"})
                continue
            files.append(path)
    files.sort(key=lambda item: item.relative_to(root).as_posix())
    return files


def scan_tree(rag_root: str | Path, scripts_root: str | Path) -> dict[str, Any]:
    """Parse Python files beneath explicit ``rag`` and ``scripts`` roots."""
    roots = (("rag", Path(rag_root)), ("scripts", Path(scripts_root)))
    errors: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    discovered = scanned = 0
    present_scopes: list[str] = []
    for scope, requested in roots:
        try:
            root = requested.resolve(strict=True)
        except (OSError, RuntimeError):
            errors.append({"path": f"{scope}/.", "kind": "source_root_missing"})
            continue
        if not root.is_dir():
            errors.append({"path": f"{scope}/.", "kind": "source_root_not_directory"})
            continue
        present_scopes.append(scope)
        files = _source_files(root, scope, errors)
        discovered += len(files)
        for path in files:
            relative = f"{scope}/{path.relative_to(root).as_posix()}"
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append({"path": relative, "kind": "source_read_error", "error_type": type(exc).__name__})
                continue
            scanned += 1
            try:
                tree = ast.parse(source, filename=relative)
            except SyntaxError as exc:
                errors.append({"path": relative, "line": exc.lineno or 1, "kind": "python_syntax_error"})
                continue
            visitor = _OwnershipCandidates(relative, scope)
            visitor.visit(tree)
            candidates.extend(visitor.candidates)
    candidates.sort(key=lambda item: (item["path"], item["line"], item["column"], item["marker"], item["owner"]))
    errors.sort(key=lambda item: (item["path"], item.get("line", 0), item["kind"]))
    return {
        "schema_version": 1,
        "source_scopes": present_scopes,
        "candidates": candidates,
        "errors": errors,
        "coverage": {
            "files_discovered": discovered,
            "files_scanned": scanned,
            "errors_count": len(errors),
            "parse_complete": not errors and len(present_scopes) == len(roots),
        },
        "writer_completeness": "not_proven",
    }


def write_report(report: dict[str, Any], output_path: str | Path) -> None:
    """Write stable UTF-8 JSON to a new path; never overwrite existing evidence."""
    with Path(output_path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rag-root", required=True, type=Path)
    parser.add_argument("--scripts-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = scan_tree(args.rag_root, args.scripts_root)
        write_report(report, args.output)
    except OSError as exc:
        print(f"ownership report write failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps({"coverage": report["coverage"], "writer_completeness": "not_proven"}, sort_keys=True))
    return 0 if report["coverage"]["parse_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
