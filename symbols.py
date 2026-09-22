"""
Deterministic symbol extraction — the "index" tier of context_tier.py.

No model runs here. That is the point. A generative summary cannot substitute
for source: measured against the originals, careful summaries scored 0.41-0.47
on "could a developer modify this without reopening the file", while "does this
point at the right symbols" scored 0.77-0.94. Summaries are good at pointing and
bad at replacing, and pointing is exactly what a parser does for free, exactly,
and without a second model in the loop.

For small files the arithmetic is even starker: a summary faithful enough to be
useful came out at 132-320% of the original file's tokens. Below a few hundred
tokens there is nothing to compress — include the file.

Python is parsed with `ast`. TypeScript and JavaScript are parsed with ast-grep
(tree-sitter). An earlier regex version is kept as a fallback for when ast-grep
is unavailable, but it is genuinely worse: it cannot see parameter lists, misses
re-exports, and matches declarations inside comments and strings.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from ast_grep_py import SgRoot
    HAVE_AST_GREP = True
except ImportError:  # pragma: no cover - exercised only where the wheel is absent
    HAVE_AST_GREP = False


@dataclass
class Symbols:
    path: str
    language: str
    exports: list[str] = field(default_factory=list)
    docline: str = ""
    parse_ok: bool = True
    parser: str = ""

    def index_entry(self, max_exports: int = 12) -> str:
        """A compact pointer: what is in this file and what it is called."""
        shown = self.exports[:max_exports]
        more = len(self.exports) - len(shown)
        parts = [self.path]
        if self.docline:
            parts.append(f" — {self.docline}")
        if shown:
            tail = f" (+{more} more)" if more > 0 else ""
            parts.append(f"\n    exports: {', '.join(shown)}{tail}")
        elif not self.parse_ok:
            parts.append(" — [unparsed]")
        return "".join(parts)


# --------------------------------------------------------------------- python


def _python(path: str, source: str) -> Symbols:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return Symbols(path, "python", parse_ok=False, parser="ast")

    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    docline = doc[0][:100] if doc else ""

    exports: list[str] = []
    explicit = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    try:
                        explicit = [str(x) for x in ast.literal_eval(node.value)]
                    except Exception:
                        pass
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            exports.append(f"{node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
            ]
            exports.append(f"class {node.name}" + (f"[{', '.join(methods[:5])}]" if methods else ""))

    if explicit:
        by_name = {e.split("(")[0].replace("class ", ""): e for e in exports}
        exports = [by_name.get(n, n) for n in explicit]

    return Symbols(path, "python", [e for e in exports if not e.startswith("_")], docline, parser="ast")


# ------------------------------------------------------------ typescript / js

_TS_LANG = {".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
            ".jsx": "tsx", ".mjs": "javascript", ".cjs": "javascript"}


def _named_child(node, kinds: tuple[str, ...]):
    for c in node.children():
        if c.kind() in kinds:
            return c
    return None


def _signature(decl) -> str:
    """`verify(token, key)` from a function declaration, params without types."""
    name = _named_child(decl, ("identifier", "type_identifier"))
    label = name.text() if name else "?"
    params = _named_child(decl, ("formal_parameters",))
    if not params:
        return label
    names: list[str] = []
    for p in params.children():
        if p.kind() in ("required_parameter", "optional_parameter"):
            ident = _named_child(p, ("identifier", "object_pattern", "array_pattern"))
            names.append(ident.text().split(":")[0].strip() if ident else "_")
        elif p.kind() == "identifier":
            names.append(p.text())
    return f"{label}({', '.join(names)})"


def _typescript_astgrep(path: str, source: str, language: str) -> Symbols:
    root = SgRoot(source, language).root()
    exports: list[str] = []

    for node in root.children():
        if node.kind() != "export_statement":
            continue
        for decl in node.children():
            kind = decl.kind()
            if kind in ("export", "default"):
                continue
            if kind == "function_declaration":
                exports.append(_signature(decl))
            elif kind == "class_declaration":
                name = _named_child(decl, ("type_identifier", "identifier"))
                body = _named_child(decl, ("class_body",))
                methods = []
                if body:
                    for m in body.children():
                        if m.kind() == "method_definition":
                            mn = _named_child(m, ("property_identifier",))
                            if mn and not mn.text().startswith("#"):
                                methods.append(mn.text())
                label = f"class {name.text() if name else '?'}"
                exports.append(label + (f"[{', '.join(methods[:5])}]" if methods else ""))
            elif kind == "lexical_declaration":
                for d in decl.children():
                    if d.kind() != "variable_declarator":
                        continue
                    n = _named_child(d, ("identifier",))
                    if not n:
                        continue
                    # `export const f = (a, b) => ...` is the dominant style in
                    # TypeScript; without reaching into the initializer the
                    # index would lose every parameter list it exists to show.
                    fn = _named_child(d, ("arrow_function", "function_expression", "generic_arrow"))
                    if fn is None:
                        for c in d.children():
                            if c.kind() in ("arrow_function", "function_expression"):
                                fn = c
                                break
                            # A generic arrow (`<E,>(x) => y`) nests one level deeper.
                            inner = _named_child(c, ("arrow_function", "function_expression"))
                            if inner is not None:
                                fn = inner
                                break
                    if fn is not None:
                        params = _named_child(fn, ("formal_parameters",))
                        if params:
                            names = []
                            for p in params.children():
                                if p.kind() in ("required_parameter", "optional_parameter"):
                                    ident = _named_child(p, ("identifier", "object_pattern", "array_pattern"))
                                    names.append(ident.text().split(":")[0].strip() if ident else "_")
                                elif p.kind() == "identifier":
                                    names.append(p.text())
                            exports.append(f"{n.text()}({', '.join(names)})")
                            continue
                    exports.append(n.text())
            elif kind in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
                n = _named_child(decl, ("type_identifier", "identifier"))
                word = {"interface_declaration": "interface",
                        "type_alias_declaration": "type",
                        "enum_declaration": "enum"}[kind]
                if n:
                    exports.append(f"{word} {n.text()}")
            elif kind == "export_clause":
                # `export { a, b } from './x'` — a re-export, which the regex
                # version could not see at all.
                for spec in decl.children():
                    if spec.kind() == "export_specifier":
                        n = _named_child(spec, ("identifier",))
                        if n:
                            exports.append(f"{n.text()} (re-export)")

    seen: set[str] = set()
    ordered = [e for e in exports if not (e in seen or seen.add(e))]
    return Symbols(path, language, ordered, _jsdoc_line(source), parser="ast-grep")


_JSDOC_FIRST = re.compile(r"/\*\*\s*\n\s*\*\s*(?:@module\s*\n\s*\*\s*)?(?P<line>[^\n@*][^\n]*)")


def _jsdoc_line(source: str) -> str:
    m = _JSDOC_FIRST.search(source)
    return m.group("line").strip()[:100] if m else ""


# Fallback only. Kept so the package degrades rather than fails when the
# ast-grep wheel is unavailable; it is measurably worse and says so.
_TS_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?"
    r"(?:(?:async\s+)?function\s+(?P<fn>\w+)"
    r"|class\s+(?P<cls>\w+)"
    r"|(?:const|let|var)\s+(?P<var>\w+)"
    r"|interface\s+(?P<iface>\w+)"
    r"|type\s+(?P<type>\w+)"
    r"|enum\s+(?P<enum>\w+))",
    re.MULTILINE,
)


def _typescript_regex(path: str, source: str) -> Symbols:
    exports: list[str] = []
    for m in _TS_EXPORT.finditer(source):
        for key, prefix in (("fn", ""), ("cls", "class "), ("var", ""),
                            ("iface", "interface "), ("type", "type "), ("enum", "enum ")):
            if m.group(key):
                exports.append(prefix + m.group(key))
                break
    seen: set[str] = set()
    ordered = [e for e in exports if not (e in seen or seen.add(e))]
    return Symbols(path, "typescript", ordered, _jsdoc_line(source), parser="regex")


def _typescript(path: str, source: str) -> Symbols:
    language = _TS_LANG.get(Path(path).suffix.lower(), "typescript")
    if HAVE_AST_GREP:
        try:
            return _typescript_astgrep(path, source, language)
        except Exception:
            pass          # malformed source or an unsupported dialect
    return _typescript_regex(path, source)


# --------------------------------------------------------------------- public

_LANGS = {".py": _python}
_LANGS.update({ext: _typescript for ext in _TS_LANG})


def extract(path: str, source: str | None = None) -> Symbols:
    """Symbols for one file. Unknown extensions get a first-line fallback."""
    if source is None:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    fn = _LANGS.get(Path(path).suffix.lower())
    if fn:
        return fn(path, source)
    first = next(
        (l.strip() for l in source.splitlines() if l.strip() and not l.lstrip().startswith("#")),
        "",
    )
    return Symbols(path, "other", [], first[:100], parse_ok=False, parser="none")
