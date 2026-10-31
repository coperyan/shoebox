"""Generate MDX API reference pages from Python source docstrings.

Walks the ``shoebox`` package with :mod:`ast` (nothing is imported, so no
credentials or config files are needed) and writes one Nextra page per
layer into ``docs/content/api/``: module docstring, public classes with
their fields and documented methods, and documented module-level
functions, each with a signature and a GitHub source link.

Run from the repo root::

    python docs/scripts/generate_api_docs.py

or from ``docs/``::

    npm run api
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PKG_ROOT = REPO_ROOT / "shoebox"
DOCS_API = REPO_ROOT / "docs" / "content" / "api"
GITHUB_BLOB = "https://github.com/coperyan/shoebox/blob/main"

# One page per layer. ``packages`` are walked recursively; ``modules`` are
# listed explicitly. Pages render in this order in the sidebar.
PAGES = {
    "core": {
        "title": "Core",
        "description": "Settings, the CLI entrypoint, the TableAsset loader and the Streamlit UI.",
        "modules": [
            "shoebox.settings",
            "shoebox.cli",
            "shoebox.storage.table_asset",
            "shoebox.ui.app",
            "shoebox.ui.helpers",
        ],
    },
    "pipelines": {
        "title": "Pipelines",
        "description": "End-to-end runnable workflows, roughly one per CLI command.",
        "packages": ["shoebox.pipelines"],
    },
    "services": {
        "title": "Services",
        "description": "Long-running components and multi-step listing workflows: ListingService, listing runs, the Slack command bot.",
        "packages": ["shoebox.services"],
    },
    "clients": {
        "title": "Clients",
        "description": "External systems: eBay REST and Trading, GCS, BigQuery, TCDB, OneDrive, Google Calendar, scrapers and saved-search state.",
        "packages": ["shoebox.clients"],
    },
    "models": {
        "title": "Models",
        "description": "Pydantic types for queue rows, image-log entries, listing results, saved searches and typed views of eBay payloads.",
        "packages": ["shoebox.models"],
    },
    "transforms": {
        "title": "Transforms",
        "description": "Pure builders: queue rows to eBay payloads, checklist normalizers, enrichment, search filters, store categories and titles.",
        "packages": ["shoebox.transforms"],
    },
    "api": {
        "title": "Listing-Queue API",
        "description": "The FastAPI service behind the iOS app: routes, dependencies, schemas and the GCS queue store.",
        "packages": ["shoebox.api"],
    },
    "utils": {
        "title": "Utilities",
        "description": "Slack messaging and formatting, pricing math, SKU hashing, JSONL helpers, image handling, logging setup.",
        "packages": ["shoebox.utils"],
    },
}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _module_path(module_name: str) -> Path:
    """Convert a dotted module name to its source file path."""
    candidate = REPO_ROOT.joinpath(*module_name.split("."))
    if candidate.with_suffix(".py").exists():
        return candidate.with_suffix(".py")
    return candidate / "__init__.py"


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _discover(package: str) -> list[str]:
    """All modules under a package, package ``__init__`` first, then sorted.

    A package ``__init__`` is only included when it carries a docstring;
    bare re-export shims add nothing to the reference.
    """
    pkg_dir = REPO_ROOT.joinpath(*package.split("."))
    modules: list[str] = []
    for path in sorted(pkg_dir.rglob("*.py")):
        if path.name == "__init__.py":
            tree = ast.parse(path.read_text())
            if not ast.get_docstring(tree):
                continue
        modules.append(_module_name(path))
    # Packages (``a.b``) sort before their children (``a.b.c``) already; keep
    # a stable name order otherwise.
    return sorted(modules)


def _page_modules(page: dict) -> list[str]:
    modules = list(page.get("modules", []))
    for pkg in page.get("packages", []):
        modules.extend(_discover(pkg))
    return modules


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _github_url(module_name: str) -> str:
    rel = _module_path(module_name).relative_to(REPO_ROOT)
    return f"{GITHUB_BLOB}/{rel.as_posix()}"


def _clean(docstring: str | None) -> str:
    if not docstring:
        return ""
    return textwrap.dedent(docstring).strip()


def _escape_mdx(text: str) -> str:
    """Escape characters MDX would read as JSX/expressions, outside code spans."""
    out: list[str] = []
    parts = text.split("`")
    for i, part in enumerate(parts):
        if i % 2 == 0:
            part = part.replace("{", "\\{").replace("}", "\\}").replace("<", "\\<")
        out.append(part)
    return "`".join(out)


def _fence(text: str) -> list[str]:
    """Wrap text in a fenced block that cannot be broken by inner backticks."""
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return [ticks, text, ticks, ""]


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef, *, method: bool = False) -> str:
    """Render ``(a: int, b: str = 'x') -> R`` from the AST, dropping ``self``/``cls``."""
    args = node.args
    if method and args.args and args.args[0].arg in {"self", "cls"}:
        args = ast.arguments(
            posonlyargs=args.posonlyargs,
            args=args.args[1:],
            vararg=args.vararg,
            kwonlyargs=args.kwonlyargs,
            kw_defaults=args.kw_defaults,
            kwarg=args.kwarg,
            defaults=args.defaults[-len(args.args[1:]) :] if args.args[1:] else [],
        )
    sig = ast.unparse(args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"({sig}){ret}"


def _decorated(node: ast.AST, name: str) -> bool:
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if ast.unparse(target).split(".")[-1] == name:
            return True
    return False


def _class_fields(node: ast.ClassDef) -> list[str]:
    """Annotated class-level attributes (Pydantic / dataclass fields)."""
    fields: list[str] = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if item.target.id.startswith("_"):
                continue
            line = f"{item.target.id}: {ast.unparse(item.annotation)}"
            if item.value is not None:
                line += f" = {ast.unparse(item.value)}"
            fields.append(line)
    return fields


def _render_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, level: int, method: bool = False
) -> list[str]:
    doc = _clean(ast.get_docstring(node))
    if not doc:
        return []
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    if method and _decorated(node, "property"):
        prefix = "property "
    elif method and _decorated(node, "classmethod"):
        prefix = "classmethod " + prefix
    elif method and _decorated(node, "staticmethod"):
        prefix = "staticmethod " + prefix
    sig = "" if "property" in prefix else _signature(node, method=method)
    lines = [f"{'#' * level} `{prefix}{node.name}{sig}`", ""]
    lines += _fence(doc)
    return lines


def _render_class(node: ast.ClassDef) -> list[str]:
    bases = ", ".join(ast.unparse(b) for b in node.bases)
    header = f"class {node.name}({bases})" if bases else f"class {node.name}"
    lines = [f"#### `{header}`", ""]
    doc = _clean(ast.get_docstring(node))
    if doc:
        lines += _fence(doc)

    fields = _class_fields(node)
    if fields:
        lines += ["**Fields**", ""]
        lines += _fence("\n".join(fields))

    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_") and item.name != "__init__":
                continue
            lines += _render_function(item, level=5, method=True)
    return lines


def _render_module(module_name: str) -> str:
    """Render a single module's documentation as MDX content."""
    lines = [f"### `{module_name}`", "", f"[Source]({_github_url(module_name)})", ""]

    src_path = _module_path(module_name)
    if not src_path.exists():
        lines.append(f"*Source file not found: {src_path}*")
        return "\n".join(lines)

    tree = ast.parse(src_path.read_text())

    module_doc = _clean(ast.get_docstring(tree))
    if module_doc:
        summary, _, rest = module_doc.partition("\n\n")
        lines.append(_escape_mdx(" ".join(summary.split())))
        lines.append("")
        if rest.strip():
            lines += _fence(rest.strip())

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            lines += _render_class(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            lines += _render_function(node, level=4)

    lines += ["---", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def generate() -> None:
    """Generate all API reference MDX files plus the section ``_meta.js``."""
    DOCS_API.mkdir(parents=True, exist_ok=True)

    overview = [
        "# API Reference",
        "",
        "Generated from the Python source docstrings by",
        "`docs/scripts/generate_api_docs.py`. Re-run it after changing a docstring;",
        "do not edit these pages by hand.",
        "",
    ]
    for slug, page in PAGES.items():
        overview.append(f"- [{page['title']}](/api/{slug}) — {page['description']}")
    overview.append("")
    (DOCS_API / "index.mdx").write_text("\n".join(overview))

    meta = ["export default {", "  index: 'Overview',"]
    for slug, page in PAGES.items():
        meta.append(f"  {slug}: '{page['title']}',")
    meta += ["}", ""]
    (DOCS_API / "_meta.js").write_text("\n".join(meta))

    for slug, page in PAGES.items():
        modules = _page_modules(page)
        content = [f"# {page['title']}", "", page["description"], ""]
        content += [f"- [`{m}`](#{m.replace('.', '')})" for m in modules]
        content.append("")
        for mod in modules:
            content.append(_render_module(mod))
        (DOCS_API / f"{slug}.mdx").write_text("\n".join(content))
        print(f"  docs/content/api/{slug}.mdx ({len(modules)} modules)")

    print(f"\nDone. {len(PAGES)} pages generated in {DOCS_API.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    generate()
