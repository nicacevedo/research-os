"""The closed arithmetic of a frozen analysis: parsed by this module, never executed.

A derived quantity -- ``wall_seconds / iterations`` for one record,
``(columns_generated - k) / p``, ``max(p_sparse, p_block)`` over three
earlier quantities -- is a string in a frozen analysis, and this is the one
place that reads it. The grammar is written out below and is the whole
language: numbers, names, the four operations, unary minus, parentheses and
five functions. Nothing here calls ``eval``, compiles Python or looks a name
up anywhere but in the mapping the caller supplies, so the text of an
expression can only ever select arithmetic this module already performs.

::

    expression := term (("+" | "-") term)*
    term       := factor (("*" | "/") factor)*
    factor     := "-" factor | primary
    primary    := NUMBER | NAME | FUNCTION "(" arguments ")" | "(" expression ")"
    arguments  := expression ("," expression)*
    FUNCTION   := log | exp | abs     (one argument)
                | min | max           (two or more)

``log`` is the natural logarithm. A NAME is a field of a record, dotted into
a nested one (``detail.columns``), or the name of an earlier quantity; which
of the two is the caller's context, never the expression's.

**Undefined, never a number.** A zero denominator, the logarithm of a
number that is not positive, an overflow, or any intermediate that is not a
finite float makes the value ``None``. So does a name the caller cannot
supply as a finite number. Nothing is clipped, defaulted or estimated.

**Bounded.** At most :data:`MAX_EXPRESSION_CHARS` characters,
:data:`MAX_NODES` nodes, :data:`MAX_DEPTH` levels of nesting and
:data:`MAX_VARIABLES` distinct names, so a frozen analysis cannot ask for
work this module did not budget for. An expression that breaks any rule is
refused when the analysis is validated -- before anything is frozen -- and
the reason names the rule.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

MAX_EXPRESSION_CHARS = 256
MAX_NODES = 64
MAX_DEPTH = 16
MAX_VARIABLES = 8

#: Each function and how many arguments it takes: an exact number, or ``-2``
#: for "two or more".
FUNCTIONS: dict[str, int] = {"log": 1, "exp": 1, "abs": 1, "min": -2, "max": -2}

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"|(?P<symbol>[-+*/(),])"
    r")"
)


class ExpressionError(ValueError):
    """An expression that is not in the language, or exceeds its bounds."""


@dataclass(frozen=True, slots=True)
class Expression:
    """A parsed expression. ``tree`` is nested tuples; ``variables`` in first-use order."""

    text: str
    tree: tuple[Any, ...]
    variables: tuple[str, ...]


def _tokens(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    position = 0
    stripped = text.rstrip()
    while position < len(stripped):
        match = _TOKEN.match(stripped, position)
        if match is None or match.end() == position:
            raise ExpressionError(
                f"{text!r} is not an expression: {stripped[position : position + 12]!r} "
                f"at character {position} is not a number, a name or one of + - * / ( ) ,"
            )
        kind = match.lastgroup
        assert kind is not None
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokens(text)
        self.position = 0
        self.nodes = 0
        self.variables: dict[str, None] = {}

    def fail(self, why: str) -> ExpressionError:
        return ExpressionError(f"{self.text!r} is not an expression: {why}")

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise self.fail("it ends where an operand is expected")
        self.position += 1
        return token

    def node(self, value: tuple[Any, ...], depth: int) -> tuple[Any, ...]:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise self.fail(f"it has more than {MAX_NODES} nodes")
        if depth > MAX_DEPTH:
            raise self.fail(f"it nests deeper than {MAX_DEPTH} levels")
        return value

    def expression(self, depth: int) -> tuple[Any, ...]:
        if depth > MAX_DEPTH:
            raise self.fail(f"it nests deeper than {MAX_DEPTH} levels")
        left = self.term(depth)
        while (token := self.peek()) is not None and token in {
            ("symbol", "+"),
            ("symbol", "-"),
        }:
            self.take()
            left = self.node(("bin", token[1], left, self.term(depth)), depth)
        return left

    def term(self, depth: int) -> tuple[Any, ...]:
        left = self.factor(depth)
        while (token := self.peek()) is not None and token in {
            ("symbol", "*"),
            ("symbol", "/"),
        }:
            self.take()
            left = self.node(("bin", token[1], left, self.factor(depth)), depth)
        return left

    def factor(self, depth: int) -> tuple[Any, ...]:
        if self.peek() == ("symbol", "-"):
            self.take()
            return self.node(("neg", self.factor(depth + 1)), depth + 1)
        return self.primary(depth)

    def primary(self, depth: int) -> tuple[Any, ...]:
        kind, value = self.take()
        if kind == "number":
            number = float(value)
            if not math.isfinite(number):
                raise self.fail(f"{value} is not a finite number")
            return self.node(("num", number), depth)
        if kind == "name":
            if value in FUNCTIONS:
                if self.peek() != ("symbol", "("):
                    raise self.fail(f"{value} is a function and takes arguments")
                self.take()
                arguments = [self.expression(depth + 1)]
                while self.peek() == ("symbol", ","):
                    self.take()
                    arguments.append(self.expression(depth + 1))
                if self.take() != ("symbol", ")"):
                    raise self.fail(f"{value}( is not closed")
                wanted = FUNCTIONS[value]
                if (wanted == -2 and len(arguments) < 2) or (
                    wanted > 0 and len(arguments) != wanted
                ):
                    count = "two or more" if wanted == -2 else str(wanted)
                    raise self.fail(
                        f"{value} takes {count} argument(s), not {len(arguments)}"
                    )
                return self.node(("call", value, tuple(arguments)), depth)
            if self.peek() == ("symbol", "("):
                raise self.fail(
                    f"{value} is not a function; the functions are "
                    f"{', '.join(sorted(FUNCTIONS))}"
                )
            self.variables.setdefault(value, None)
            if len(self.variables) > MAX_VARIABLES:
                raise self.fail(f"it names more than {MAX_VARIABLES} variables")
            return self.node(("var", value), depth)
        if (kind, value) == ("symbol", "("):
            inner = self.expression(depth + 1)
            if self.take() != ("symbol", ")"):
                raise self.fail("a parenthesis is not closed")
            return inner
        raise self.fail(f"{value!r} cannot start an operand")


@lru_cache(maxsize=1024)
def parse(text: str) -> Expression:
    """Parse one expression, or raise :class:`ExpressionError` saying why not."""

    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("an expression must not be empty")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise ExpressionError(
            f"an expression is at most {MAX_EXPRESSION_CHARS} characters"
        )
    parser = _Parser(text)
    tree = parser.expression(1)
    if parser.peek() is not None:
        raise parser.fail(f"{parser.peek()[1]!r} follows a complete expression")  # type: ignore[index]
    return Expression(text=text, tree=tree, variables=tuple(parser.variables))


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _value(
    node: tuple[Any, ...], lookup: Callable[[str], float | None]
) -> float | None:
    kind = node[0]
    if kind == "num":
        return float(node[1])
    if kind == "var":
        found = lookup(node[1])
        if found is None or isinstance(found, bool):
            return None
        if not isinstance(found, int | float):
            return None
        try:
            converted = float(found)
        except OverflowError:
            return None
        return _finite(converted)
    if kind == "neg":
        inner = _value(node[1], lookup)
        return None if inner is None else -inner
    if kind == "bin":
        left = _value(node[2], lookup)
        if left is None:
            return None
        right = _value(node[3], lookup)
        if right is None:
            return None
        operator = node[1]
        if operator == "+":
            return _finite(left + right)
        if operator == "-":
            return _finite(left - right)
        if operator == "*":
            return _finite(left * right)
        if right == 0.0:
            return None
        return _finite(left / right)
    if kind == "call":
        values = [_value(item, lookup) for item in node[2]]
        if any(item is None for item in values):
            return None
        numbers = [float(item) for item in values if item is not None]
        name = node[1]
        if name == "log":
            return math.log(numbers[0]) if numbers[0] > 0.0 else None
        if name == "exp":
            try:
                return _finite(math.exp(numbers[0]))
            except OverflowError:
                return None
        if name == "abs":
            return abs(numbers[0])
        if name == "min":
            return min(numbers)
        if name == "max":
            return max(numbers)
    raise ExpressionError(
        f"unknown node {kind!r}"
    )  # pragma: no cover - parse builds these


def evaluate(
    expression: Expression, lookup: Callable[[str], float | None]
) -> float | None:
    """The value of a parsed expression, or ``None`` when it is undefined."""

    return _value(expression.tree, lookup)


__all__ = [
    "FUNCTIONS",
    "MAX_DEPTH",
    "MAX_EXPRESSION_CHARS",
    "MAX_NODES",
    "MAX_VARIABLES",
    "Expression",
    "ExpressionError",
    "evaluate",
    "parse",
]
