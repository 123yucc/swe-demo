"""
AST-based structural grounding (phase 25, gap A — pure code, no LLM).

``grounding.py`` proves *existence* of symbols/regions via grep + line ranges.
This module proves *structure*: that a claimed caller→callee edge exists, that
a def-use pair is reachable within a scope, and that a symbol/exception named in
a symptom actually has a definition point. These are exactly the three fields
the design table (phase25 doc, §63-77) earmarked for the strongest grounding —
``localization.call_chain_context``, ``localization.dataflow_relevant_uses``,
and ``symptom.observable_failures`` — without executing any code.

Backends:
  * Python (.py)  — stdlib ``ast``, zero dependency, the baseline backend.
  * tree-sitter   — optional, for .go/.js/.ts/.java. Loaded lazily; if the
                    library or a grammar is unavailable, the language degrades
                    to "unsupported" and callers fall back to grep (soft pass).

Hard rule (mirrors grounding.py and the dynamic-grounding §4 stance): AST only
turns a *definite structural refutation* into a fail. Parse failure, unsupported
language, or insufficient information is always a soft pass — never a fail — so
AST brittleness can't misfire. Callers tag soft passes with ``grounded_by`` so
the distinction between "verified" and "could not verify" is never lost.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


# ── Symbol index ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DefSite:
    name: str
    lineno: int
    scope: str  # enclosing qualified name ("" = module level)
    kind: str   # "function" | "class"
    end_lineno: int = 0  # last line of the def body (0 = unknown)


@dataclass(frozen=True)
class CallSite:
    callee_name: str
    lineno: int
    enclosing_def: str  # qualified name of the function/method making the call


@dataclass(frozen=True)
class NameUse:
    name: str
    lineno: int
    ctx: str  # "load" | "store"
    scope: str


@dataclass
class SymbolIndex:
    """Language-agnostic structural index of one source file."""

    language: str
    defs: list[DefSite] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    names: list[NameUse] = field(default_factory=list)

    def def_names(self) -> set[str]:
        return {d.name for d in self.defs}


# ── Python backend (stdlib ast) ────────────────────────────────────────────

class _PyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.defs: list[DefSite] = []
        self.calls: list[CallSite] = []
        self.names: list[NameUse] = []
        self._scope_stack: list[str] = []

    @property
    def _scope(self) -> str:
        return ".".join(self._scope_stack)

    def _visit_def(self, node, kind: str) -> None:
        self.defs.append(DefSite(
            name=node.name, lineno=node.lineno, scope=self._scope, kind=kind,
            end_lineno=getattr(node, "end_lineno", 0) or 0,
        ))
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_def(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_def(node, "function")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_def(node, "class")

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        callee = _callee_name(node.func)
        if callee:
            self.calls.append(CallSite(
                callee_name=callee, lineno=node.lineno, enclosing_def=self._scope,
            ))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        ctx = "store" if isinstance(node.ctx, (ast.Store, ast.Del)) else "load"
        self.names.append(NameUse(
            name=node.id, lineno=node.lineno, ctx=ctx, scope=self._scope,
        ))
        self.generic_visit(node)


def _callee_name(func: ast.expr) -> str:
    """Last identifier of a call target: ``a.b.c()`` → ``c``, ``f()`` → ``f``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _build_python_index(source: str) -> SymbolIndex | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    v = _PyVisitor()
    v.visit(tree)
    return SymbolIndex(
        language="python", defs=v.defs, calls=v.calls, names=v.names,
    )


# ── tree-sitter backend (optional, lazy) ───────────────────────────────────

_TS_LANG_BY_SUFFIX = {
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
}


def _build_treesitter_index(source: str, language: str) -> SymbolIndex | None:
    """Best-effort tree-sitter index. Returns None when unavailable.

    tree-sitter is an optional dependency; any import / grammar-load failure
    degrades the language to unsupported (caller falls back to grep soft pass).
    """
    try:
        from tree_sitter_languages import get_parser  # type: ignore
    except Exception:
        return None
    try:
        parser = get_parser(language)
        tree = parser.parse(source.encode("utf-8", errors="replace"))
    except Exception:
        return None

    defs: list[DefSite] = []
    calls: list[CallSite] = []
    names: list[NameUse] = []

    # Generic, grammar-tolerant walk: collect definition and call nodes by
    # node-type substring so we don't hard-code each grammar's full schema.
    def _node_text(node) -> str:
        return source[node.start_byte:node.end_byte]

    def _name_child(node) -> str:
        for child in node.children:
            if child.type in ("identifier", "field_identifier", "type_identifier", "name"):
                return _node_text(child)
        return ""

    def _walk(node, scope: str) -> None:
        ntype = node.type
        new_scope = scope
        if "function" in ntype or "method" in ntype:
            nm = _name_child(node)
            if nm:
                defs.append(DefSite(
                    name=nm, lineno=node.start_point[0] + 1,
                    scope=scope, kind="function",
                    end_lineno=node.end_point[0] + 1,
                ))
                new_scope = f"{scope}.{nm}" if scope else nm
        elif ntype in ("class_declaration", "class_definition", "type_declaration"):
            nm = _name_child(node)
            if nm:
                defs.append(DefSite(
                    name=nm, lineno=node.start_point[0] + 1,
                    scope=scope, kind="class",
                    end_lineno=node.end_point[0] + 1,
                ))
                new_scope = f"{scope}.{nm}" if scope else nm
        elif "call" in ntype:
            callee = _ts_callee_name(node, source)
            if callee:
                calls.append(CallSite(
                    callee_name=callee, lineno=node.start_point[0] + 1,
                    enclosing_def=scope,
                ))
        for child in node.children:
            _walk(child, new_scope)

    try:
        _walk(tree.root_node, "")
    except Exception:
        return None
    return SymbolIndex(language=language, defs=defs, calls=calls, names=names)


def _ts_callee_name(call_node, source: str) -> str:
    """Extract the callee identifier from a tree-sitter call node."""
    func = call_node.child_by_field_name("function")
    target = func if func is not None else (
        call_node.children[0] if call_node.children else None
    )
    if target is None:
        return ""
    text = source[target.start_byte:target.end_byte]
    # ``a.b.c`` → ``c``; ``pkg.Fn`` → ``Fn``; bare ``f`` → ``f``.
    return text.split(".")[-1].split("::")[-1].strip("() ")


# ── Public builder ─────────────────────────────────────────────────────────

def build_symbol_index(path: str, source: str) -> SymbolIndex | None:
    """Dispatch to a language backend by file extension.

    Returns None on unsupported language or parse failure — the caller MUST
    treat None as "could not verify" and fall back to grep (soft pass), never
    as a failure.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return _build_python_index(source)
    language = _TS_LANG_BY_SUFFIX.get(suffix)
    if language is None:
        return None
    return _build_treesitter_index(source, language)


# ── Language-agnostic queries ──────────────────────────────────────────────

def has_symbol_def(index: SymbolIndex, name: str) -> bool:
    """True if *name* has a function/class definition point in this file."""
    return any(d.name == name for d in index.defs)


def has_exception_class(index: SymbolIndex, name: str) -> bool:
    """True if *name* is defined as a class (proxy for an exception type)."""
    return any(d.name == name and d.kind == "class" for d in index.defs)


def has_call_edge(index: SymbolIndex, caller_name: str, callee_name: str) -> bool:
    """True if a call to *callee_name* occurs inside *caller_name*'s body.

    Scope match is by suffix of the enclosing qualified name so ``Class.method``
    matches a caller cited as bare ``method``.
    """
    for call in index.calls:
        if call.callee_name != callee_name:
            continue
        scope_tail = call.enclosing_def.split(".")[-1] if call.enclosing_def else ""
        if call.enclosing_def == caller_name or scope_tail == caller_name:
            return True
    return False


def def_spans_containing(index: SymbolIndex, line: int) -> list[DefSite]:
    """Return def sites whose ``[lineno, end_lineno]`` span contains *line*.

    Used by dynamic grounding to map an executed source line back to the
    function/method it belongs to (function-granularity matching, robust to
    line drift). Defs with an unknown end (``end_lineno == 0``) are skipped.
    """
    hits: list[DefSite] = []
    for d in index.defs:
        if d.end_lineno and d.lineno <= line <= d.end_lineno:
            hits.append(d)
    return hits


def resolves_def_use(
    index: SymbolIndex,
    var_name: str,
    def_line: int,
    use_line: int,
) -> bool:
    """True if *var_name* has a store at/near *def_line* and a load at *use_line*.

    Line matching is tolerant (±0 exact preferred, store must precede or equal
    use). A conservative structural check: both endpoints must exist as the
    right context. Missing either endpoint → False (caller treats as soft
    signal, not a hard fail; see grounding wiring).
    """
    has_store = any(
        n.name == var_name and n.ctx == "store" and n.lineno <= use_line
        for n in index.names
    )
    has_load = any(
        n.name == var_name and n.ctx == "load" and n.lineno == use_line
        for n in index.names
    )
    if not has_load:
        # Be tolerant: any load of the name after the def line also counts.
        has_load = any(
            n.name == var_name and n.ctx == "load" and n.lineno >= def_line
            for n in index.names
        )
    return has_store and has_load
