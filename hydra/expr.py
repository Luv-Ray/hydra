"""Template expansion and safe expression evaluation for Hydra DSL."""

from __future__ import annotations

import ast
import re

from .errors import TemplateError

_TEMPLATE_RE = re.compile(r"\{\{\s*inputs\.(\w+)\s*\}\}")


def expand_template(text: str, inputs: dict[str, str]) -> str:
    """Replace {{ inputs.xxx }} placeholders with values from inputs dict."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key not in inputs:
            raise TemplateError(f"Undefined input variable: inputs.{key}")
        return inputs[key]

    return _TEMPLATE_RE.sub(_replace, text)


def eval_when(expr: str, inputs: dict[str, str]) -> bool:
    """Safely evaluate a when expression. Only allows comparisons and boolean ops."""
    stripped = expr.strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        stripped = stripped[2:-2].strip()

    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as e:
        raise TemplateError(f"Invalid when expression: {expr!r}") from e

    return bool(_eval_node(tree.body, inputs))


def _eval_node(node: ast.expr, inputs: dict[str, str]) -> object:
    match node:
        case ast.Constant(value=v):
            return v
        case ast.Attribute(value=ast.Name(id="inputs"), attr=name):
            return inputs.get(name, "")
        case ast.Compare(left=left, ops=ops, comparators=comps):
            result = _eval_node(left, inputs)
            for op, comp in zip(ops, comps):
                comp_val = _eval_node(comp, inputs)
                if not _compare(op, result, comp_val):
                    return False
                result = comp_val
            return True
        case ast.BoolOp(op=op, values=values):
            if isinstance(op, ast.And):
                return all(_eval_node(v, inputs) for v in values)
            else:
                return any(_eval_node(v, inputs) for v in values)
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            return not _eval_node(operand, inputs)
        case ast.Name(id="True"):
            return True
        case ast.Name(id="False"):
            return False
        case _:
            raise TemplateError(
                f"Unsupported expression node: {ast.dump(node)}"
            )


def _compare(op: ast.cmpop, left: object, right: object) -> bool:
    match op:
        case ast.Eq():
            return left == right
        case ast.NotEq():
            return left != right
        case ast.In():
            return left in right  # type: ignore[operator]
        case ast.NotIn():
            return left not in right  # type: ignore[operator]
        case _:
            raise TemplateError(f"Unsupported comparison operator: {type(op).__name__}")
