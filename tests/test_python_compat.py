import ast
from pathlib import Path


def _source_files():
    return sorted(Path("src").rglob("*.py"))


def _has_pep604_annotation(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        annotation = getattr(node, "annotation", None)
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return True
    return False


def _has_future_annotations(tree: ast.AST) -> bool:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            return any(alias.name == "annotations" for alias in node.names)
    return False


def test_source_parses_with_declared_python_39_grammar():
    for path in _source_files():
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))


def test_pep604_annotations_are_postponed_for_python_39_runtime():
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        if _has_pep604_annotation(tree):
            assert _has_future_annotations(tree), str(path)
