import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIT_ROOTS = [ROOT / "src" / "koval"]
FORBIDDEN_IMPORT_PREFIXES = (
    "backtrader",
    "koval.adapters.backtrader",
    "koval_backtrader",
)


def _mit_py_files():
    for base in MIT_ROOTS:
        yield from base.rglob("*.py")


def _package_name(path: Path) -> str:
    relative = path.relative_to(ROOT / "src").with_suffix("")
    return ".".join(relative.parts[:-1])


def _resolved_from_module(node: ast.ImportFrom, package_name: str | None) -> str:
    module_parts = (node.module or "").split(".") if node.module else []
    if node.level == 0 or package_name is None:
        return ".".join(module_parts)
    package_parts = package_name.split(".") if package_name else []
    parents_to_drop = node.level - 1
    if parents_to_drop > len(package_parts):
        return ".".join(module_parts)
    base_parts = package_parts[: len(package_parts) - parents_to_drop]
    return ".".join([*base_parts, *module_parts])


def _imports_gpl_code(source: str, *, package_name: str | None = None) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == prefix or alias.name.startswith(f"{prefix}.")
                for alias in node.names
                for prefix in FORBIDDEN_IMPORT_PREFIXES
            ):
                return True
        if isinstance(node, ast.ImportFrom):
            module = _resolved_from_module(node, package_name)
            imported_names = [module]
            imported_names.extend(
                f"{module}.{alias.name}" if module else alias.name for alias in node.names
            )
            for imported_name in imported_names:
                if any(
                    imported_name == prefix or imported_name.startswith(f"{prefix}.")
                    for prefix in FORBIDDEN_IMPORT_PREFIXES
                ):
                    return True
    return False


def test_gpl_import_detector_catches_dependency_and_adapter_namespace():
    assert _imports_gpl_code("import backtrader as bt")
    assert _imports_gpl_code("from koval.adapters.backtrader import bt_adapter")
    assert _imports_gpl_code("from koval.adapters import backtrader")
    assert _imports_gpl_code(
        "from ..adapters import backtrader",
        package_name="koval.engine",
    )


def test_no_mit_file_imports_backtrader_or_in_tree_adapter():
    offenders = [
        str(p.relative_to(ROOT))
        for p in _mit_py_files()
        if _imports_gpl_code(
            p.read_text(encoding="utf-8"),
            package_name=_package_name(p),
        )
    ]
    assert offenders == [], f"MIT code must not import GPL Backtrader code: {offenders}"
