"""Template expansion and the safe expression language used by `when`/`until`.

Expression grammar (parsed with `ast`, everything else rejected):
  - references:  inputs.NAME          → resolved input value (str, "" if unset)
                 steps.ID.verdict     → that step's verdict (str) or None if it
                                        has not produced one (skipped / not run)
  - literals:    strings, numbers, True/False/None, [list] / (tuple) of literals
  - operators:   == != in "not in", and / or / not
An optional `{{ ... }}` wrapper is stripped.
"""

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


def parse_expr(expr: str) -> ast.expr:
    """Parse and structurally validate an expression. Raises TemplateError."""
    stripped = expr.strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        stripped = stripped[2:-2].strip()
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as e:
        raise TemplateError(f"Invalid expression: {expr!r}") from e
    _validate(tree.body, expr)
    return tree.body


def _ref(node: ast.expr) -> tuple[str, str] | None:
    """('inputs', name) / ('steps', id) for a reference node, else None."""
    match node:
        case ast.Attribute(value=ast.Name(id="inputs"), attr=name):
            return "inputs", name
        case ast.Attribute(
            value=ast.Attribute(value=ast.Name(id="steps"), attr=sid), attr="verdict"
        ):
            return "steps", sid
    return None


def _validate(node: ast.expr, expr: str) -> None:
    if _ref(node) is not None:
        return
    match node:
        case ast.Constant():
            return
        case ast.List(elts=elts) | ast.Tuple(elts=elts):
            for e in elts:
                if not isinstance(e, ast.Constant):
                    raise TemplateError(f"Only literals are allowed inside a list: {expr!r}")
            return
        case ast.Compare(left=left, ops=ops, comparators=comps):
            for op in ops:
                if not isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                    raise TemplateError(
                        f"Unsupported operator {type(op).__name__} in {expr!r}"
                    )
            for n in (left, *comps):
                _validate(n, expr)
            return
        case ast.BoolOp(values=values):
            for v in values:
                _validate(v, expr)
            return
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            _validate(operand, expr)
            return
        case ast.Name(id="True" | "False" | "None"):
            return
    raise TemplateError(
        f"Unsupported construct in {expr!r}: only inputs.NAME, steps.ID.verdict, "
        f"literals, == != in, and/or/not are allowed"
    )


def expr_refs(node: ast.expr) -> tuple[set[str], set[str]]:
    """(input names, step ids) referenced by an expression."""
    inputs: set[str] = set()
    steps: set[str] = set()
    for n in ast.walk(node):
        r = _ref(n)
        if r:
            (inputs if r[0] == "inputs" else steps).add(r[1])
    return inputs, steps


def verdict_literals(node: ast.expr) -> list[tuple[str, object]]:
    """(step id, literal) for every literal compared against steps.ID.verdict.

    Lets validate catch `steps.review.verdict == 'ACCPET'` — a typo that would
    otherwise just never match.
    """
    found: list[tuple[str, object]] = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Compare):
            continue
        operands = [n.left, *n.comparators]
        for a, b in zip(operands, operands[1:]):
            for ref_side, lit_side in ((a, b), (b, a)):
                r = _ref(ref_side)
                if not r or r[0] != "steps":
                    continue
                if isinstance(lit_side, ast.Constant):
                    found.append((r[1], lit_side.value))
                elif isinstance(lit_side, (ast.List, ast.Tuple)):
                    found.extend((r[1], e.value) for e in lit_side.elts)  # type: ignore[attr-defined]
    return found


def eval_expr(
    node: ast.expr, inputs: dict[str, str], verdicts: dict[str, str | None]
) -> bool:
    return bool(_eval(node, inputs, verdicts))


def _eval(node: ast.expr, inputs: dict[str, str], verdicts: dict[str, str | None]) -> object:
    r = _ref(node)
    if r is not None:
        return inputs.get(r[1], "") if r[0] == "inputs" else verdicts.get(r[1])
    match node:
        case ast.Constant(value=v):
            return v
        case ast.List(elts=elts) | ast.Tuple(elts=elts):
            return [_eval(e, inputs, verdicts) for e in elts]
        case ast.Compare(left=left, ops=ops, comparators=comps):
            cur = _eval(left, inputs, verdicts)
            for op, comp in zip(ops, comps):
                nxt = _eval(comp, inputs, verdicts)
                if not _compare(op, cur, nxt):
                    return False
                cur = nxt
            return True
        case ast.BoolOp(op=op, values=values):
            vals = (_eval(v, inputs, verdicts) for v in values)
            return all(vals) if isinstance(op, ast.And) else any(vals)
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            return not _eval(operand, inputs, verdicts)
        case ast.Name(id=name):
            return {"True": True, "False": False, "None": None}[name]
    raise TemplateError(f"Unsupported expression node: {ast.dump(node)}")


def _compare(op: ast.cmpop, left: object, right: object) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    # `in` against a non-container (e.g. a None verdict) is simply False.
    try:
        hit = left in right  # type: ignore[operator]
    except TypeError:
        hit = False
    return hit if isinstance(op, ast.In) else not hit
