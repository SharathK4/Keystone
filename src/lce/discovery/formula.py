"""Rules as safe, readable formulas.

A rule is one arithmetic expression over named clues, for example
``need * importance / (1 + steps)``. Text from a language model is parsed with
:mod:`ast` and checked node by node against a whitelist before anything is
evaluated: numbers, clue names, ``+ - * /``, a constant power, ``min``,
``max``, ``log``, ``sqrt``, ``abs`` and single comparisons (which give 1 or 0).
No attribute access, no subscripts, no names outside the clue list, no calls
outside the five functions. Nothing is passed to ``eval``.

Evaluation is vectorised over every merchant in a scenario and protected the
way genetic programming protects it: ``log(x)`` is ``log(1 + |x|)``,
``sqrt(x)`` is ``sqrt(|x|)``, division by (almost) zero divides by a tiny
number instead, and a merchant whose score is not finite is ranked last.
"""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from lce.errors import ValidationError

MAX_CHARS = 240
MAX_SIZE = 40
MAX_CONSTANT = 1e12
MAX_EXPONENT = 4.0

FUNCTIONS = {"log": 1, "sqrt": 1, "abs": 1, "min": 2, "max": 2}
_BIN = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "**"}
_CMP = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}


class FormulaError(ValidationError):
    """The text is not a rule the lab can run, with the reason in plain words."""


@dataclass(frozen=True, slots=True)
class Formula:
    """A checked rule: its canonical text, its tree, and what it reads."""

    text: str
    node: ast.expr
    size: int
    clues: tuple[str, ...]

    def tree(self) -> dict[str, Any]:
        """The expression as JSON, for typesetting on the page."""
        return _tree(self.node)

    def score(self, clues: Mapping[str, np.ndarray]) -> np.ndarray:
        """Score every merchant; anything not finite becomes ``-inf``."""
        with np.errstate(all="ignore"):
            raw = np.asarray(_eval(self.node, clues), dtype=float)
        n = len(next(iter(clues.values())))
        if raw.ndim == 0:
            raw = np.full(n, float(raw))
        return np.where(np.isfinite(raw), raw, -np.inf)


def parse(text: str, allowed: frozenset[str] | set[str]) -> Formula:
    """Check ``text`` and return the rule, or raise :class:`FormulaError`."""
    source = (text or "").strip().replace("^", "**").replace(chr(0xD7), "*").replace(chr(0xF7), "/")
    source = source.removeprefix("score =").removeprefix("score=").strip().rstrip(";").strip()
    if not source:
        raise FormulaError("the rule is empty")
    if len(source) > MAX_CHARS:
        raise FormulaError(f"the rule is longer than {MAX_CHARS} characters")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"not a formula ({exc.msg})") from exc
    used: list[str] = []
    size = _check(tree.body, allowed, used)
    if size > MAX_SIZE:
        raise FormulaError(f"too long: {size} symbols, the limit is {MAX_SIZE}")
    if not used:
        raise FormulaError("the rule reads no clue, so it cannot tell merchants apart")
    return Formula(text=ast.unparse(tree.body), node=tree.body, size=size,
                   clues=tuple(dict.fromkeys(used)))


def _check(node: ast.AST, allowed: frozenset[str] | set[str], used: list[str]) -> int:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise FormulaError("only numbers may appear as constants")
        if not math.isfinite(float(value)) or abs(float(value)) > MAX_CONSTANT:
            raise FormulaError("a constant is too large")
        return 1
    if isinstance(node, ast.Name):
        if node.id not in allowed:
            raise FormulaError(f"'{node.id}' is not one of the clues")
        used.append(node.id)
        return 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        return 1 + _check(node.operand, allowed, used)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        if isinstance(node.op, ast.Pow):
            exponent = node.right
            if isinstance(exponent, ast.UnaryOp) and isinstance(exponent.op, ast.USub):
                exponent = exponent.operand
            if not (isinstance(exponent, ast.Constant)
                    and isinstance(exponent.value, int | float)
                    and abs(float(exponent.value)) <= MAX_EXPONENT):
                raise FormulaError(f"a power must be a number between -{MAX_EXPONENT:g} "
                                   f"and {MAX_EXPONENT:g}")
        return 1 + _check(node.left, allowed, used) + _check(node.right, allowed, used)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or type(node.ops[0]) not in _CMP:
            raise FormulaError("use one comparison at a time (<, <=, >, >=)")
        return 1 + _check(node.left, allowed, used) + _check(node.comparators[0], allowed, used)
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name not in FUNCTIONS:
            raise FormulaError("the only functions are log, sqrt, abs, min and max")
        if node.keywords or len(node.args) != FUNCTIONS[name]:
            raise FormulaError(f"{name} takes {FUNCTIONS[name]} argument"
                               f"{'s' if FUNCTIONS[name] > 1 else ''}")
        return 1 + sum(_check(a, allowed, used) for a in node.args)
    raise FormulaError("only arithmetic, comparisons and log, sqrt, abs, min, max are allowed")


def _eval(node: ast.expr, clues: Mapping[str, np.ndarray]) -> Any:
    if isinstance(node, ast.Constant):
        return float(node.value)  # type: ignore[arg-type]  # numbers only, after _check
    if isinstance(node, ast.Name):
        return clues[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, clues)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, clues), _eval(node.right, clues)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            b = np.where(np.abs(b) < 1e-9, np.where(b < 0, -1e-9, 1e-9), b)
            return a / b
        return np.sign(a) * np.abs(a) ** b  # a real power of a negative stays real
    if isinstance(node, ast.Compare):
        a, b = _eval(node.left, clues), _eval(node.comparators[0], clues)
        op = node.ops[0]
        if isinstance(op, ast.Lt):
            return np.asarray(a < b, dtype=float)
        if isinstance(op, ast.LtE):
            return np.asarray(a <= b, dtype=float)
        if isinstance(op, ast.Gt):
            return np.asarray(a > b, dtype=float)
        return np.asarray(a >= b, dtype=float)
    if isinstance(node, ast.Call):
        name = node.func.id  # type: ignore[attr-defined]
        args = [_eval(a, clues) for a in node.args]
        if name == "log":
            return np.log1p(np.abs(args[0]))
        if name == "sqrt":
            return np.sqrt(np.abs(args[0]))
        if name == "abs":
            return np.abs(args[0])
        if name == "min":
            return np.minimum(args[0], args[1])
        return np.maximum(args[0], args[1])
    raise FormulaError("unsupported expression")  # unreachable after _check


def _tree(node: ast.expr) -> dict[str, Any]:
    if isinstance(node, ast.Constant):
        return {"t": "num", "v": float(node.value)}  # type: ignore[arg-type]
    if isinstance(node, ast.Name):
        return {"t": "clue", "name": node.id}
    if isinstance(node, ast.UnaryOp):
        inner = _tree(node.operand)
        return {"t": "neg", "a": inner} if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.BinOp):
        return {"t": "bin", "op": _BIN[type(node.op)], "a": _tree(node.left),
                "b": _tree(node.right)}
    if isinstance(node, ast.Compare):
        return {"t": "cmp", "op": _CMP[type(node.ops[0])], "a": _tree(node.left),
                "b": _tree(node.comparators[0])}
    if isinstance(node, ast.Call):
        return {"t": "fn", "name": node.func.id,  # type: ignore[attr-defined]
                "args": [_tree(a) for a in node.args]}
    raise FormulaError("unsupported expression")
