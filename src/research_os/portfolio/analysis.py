"""The one place a frozen analysis is evaluated. Ordinary Python; no model.

A scientific contract's analysis half (:class:`~research_os.portfolio.
contracts.AnalysisSpec`) is *filled in* by a model before any result exists
and *evaluated* here after one does. Nothing in this module can be steered by
the text of the thing it evaluates: every operation is a member of a closed
set, the arithmetic is written out below, and the only inputs are the frozen
specification and the bytes the run wrote.

The order of evaluation is the honesty, and it is the same order the route
always used, extended:

1. every observable the analysis reads must be present and readable, or the
   conclusion is ``INSUFFICIENT`` and says which one was not;
2. records are filtered by the frozen inclusion rules, and a record missing a
   required field is either excluded (counted) or makes the whole analysis
   ``INSUFFICIENT`` -- whichever the contract fixed in advance;
3. the data must exhibit what the contract said it must (enough records,
   enough distinct values of the fields the statistic depends on), or the
   conclusion is ``INSUFFICIENT``: this is the defence against a design that
   determines its own statistic, checked against what was *measured* rather
   than against what was planned;
4. the reductions are computed in order; a quantity that is undefined -- an
   empty selection, a zero denominator, a regression the design does not
   identify -- is ``None``, and a primary statistic that is ``None`` is
   ``INSUFFICIENT``;
5. the uncertainty, when the contract asked for one, is a seeded percentile
   bootstrap over the analysed records;
6. the two prespecified predicates are applied to the statistic -- and, when
   there is an interval, to both of its ends: ``SUPPORTS`` only when the whole
   interval satisfies the success predicate and none of it the failure one.

The second analysis language (``docs/SCIENCE_EXECUTION.md`` §4a) adds a step
between 2 and 3 -- **tables**, records derived from records in order:
grouped and aggregated, given computed fields, filtered -- and, among the
reductions, rank and partial correlations, the crossing of a curve, closed
arithmetic over earlier quantities and a seeded permutation null. A table
derives; it never observes: every number in it is computed here from the
records the run wrote. Everything above holds for a table as for an
observable -- incomplete records, undecidable selections, support over every
selection -- and an analysis written in the first language is evaluated
exactly as before.

Nothing here estimates, imputes, or substitutes. ``on_missing`` is
``INSUFFICIENT`` in every contract because it has no other value.
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from research_os.portfolio import expressions
from research_os.portfolio.contracts import (
    PARTIAL_OPERATIONS,
    SCALAR_OPERATIONS,
    AnalysisSpec,
    Condition,
    ContractError,
    Observable,
    Reduction,
    Table,
)
from research_os.portfolio.models import EmpiricalConclusion

#: The largest raw output this module will parse. A larger result is still
#: collected and hashed by the empirical route; it is not analysed in a work
#: item, and the conclusion says so rather than reading part of it.
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024

#: The most records one observable may hold.
MAX_RECORDS = 200_000

#: The most record draws one bootstrap may make (resamples x records). A
#: contract asking for more is refused at evaluation time as INSUFFICIENT --
#: never silently thinned to fit.
MAX_BOOTSTRAP_DRAWS = 20_000_000

#: The share of bootstrap resamples that must produce a finite statistic for
#: the interval to be reported. Below it the interval would describe the
#: resamples that happened to work, which is not the data's uncertainty.
MIN_FINITE_RESAMPLES = 0.9

#: How close to singular a regression's normal equations may be.
SINGULAR_TOLERANCE = 1e-10

#: The fewest analysed records a percentile bootstrap may resample. Below it
#: the interval is an artefact of the resampling, not of the data -- an
#: independent review measured a one-record "interval" of [1.01, 1.01]
#: clearing a threshold -- and the conclusion is INSUFFICIENT.
MIN_BOOTSTRAP_RECORDS = 10

#: Below this many records a bootstrap distribution with no spread at all is
#: read as insufficient rather than as certainty: five of five successes
#: resample to p = 1 every time, and the exact interval would reach ~0.48.
MIN_DEGENERATE_RECORDS = 30

#: The most record operations one resampling -- a bootstrap over tables, or a
#: permutation null -- may make in one work item: draws times an upper bound
#: on the records each draw recomputes. Beyond it the quantity is undefined
#: and the conclusion says so; it is never thinned to fit.
MAX_RESAMPLING_WORK = MAX_BOOTSTRAP_DRAWS

#: How close, relative to the observed statistic, a permuted one counts as a
#: tie. A tie counts as at least as extreme as the observation, so the
#: p-value can only rise: the same multiset of values summed in another order
#: is the same statistic, and rounding must not make it a smaller one.
PERMUTATION_TIE_TOLERANCE = 1e-12

#: The evaluator's own identity, recorded with every reading. The digests
#: bind the *specification*; this binds the semantics that read it, so a
#: later change to how an analysis is evaluated is visible beside every
#: conclusion reached before it. Version 2 reads the second analysis
#: language as well; an analysis in the first is read exactly as version 1
#: read it (tests/test_analysis_operations_v2.py holds that, against analyses
#: frozen by version 1).
ENGINE_VERSION = "portfolio.analysis@2"


@dataclass(frozen=True, slots=True)
class Unavailable:
    """A raw output the analysis needed and could not read, and why."""

    reason: str


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """What the frozen analysis concluded, and every number it read to get there."""

    conclusion: EmpiricalConclusion
    summary: str
    statistic: float | None = None
    interval: tuple[float, float] | None = None
    statistics: Mapping[str, float | None] = field(default_factory=dict)
    records: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    support: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        """The part of the analysis document this module is responsible for."""

        return {
            "engine": ENGINE_VERSION,
            "conclusion": str(self.conclusion),
            "primary_statistic_value": self.statistic,
            "interval": list(self.interval) if self.interval else None,
            "statistics": dict(self.statistics),
            "records": {name: dict(value) for name, value in self.records.items()},
            "support": [dict(item) for item in self.support],
            "notes": list(self.notes),
        }


class _Undefined(Exception):
    """A reduction has no value for this data. Carries the reason."""


# ---------------------------------------------------------------- loading --
class _NotANumber(ValueError):
    pass


def _refuse_constant(literal: str) -> float:
    raise _NotANumber(literal)


def load_document(path: Path) -> Any:
    """Parse one raw output, or say why it could not be.

    JSON is parsed strictly -- ``NaN`` and the infinities are refused, as
    :func:`research_os.portfolio.empirical.metric_from` refuses them -- and
    a ``.csv`` or ``.tsv`` file becomes a list of records whose values are
    numbers where they parse as finite numbers and strings otherwise. An
    empty cell is an absent field, never zero.
    """

    try:
        if path.is_symlink() or not path.is_file():
            return Unavailable(f"{path.name} was not written")
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            return Unavailable(
                f"{path.name} is larger than {MAX_DOCUMENT_BYTES} bytes and is "
                f"not analysed here"
            )
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Unavailable(f"{path.name} could not be read: {exc}")
    return parse_document(text, name=path.name)


def parse_bytes(data: bytes, *, name: str) -> Any:
    """Parse one raw output already read -- by a contained reader -- as bytes.

    What :func:`load_document` does after it has opened the file, for a caller
    that opened it through ``research_os.automation.filescope.open_contained``
    and checked the bytes against the hash the evidence records.
    """

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Unavailable(f"{name} could not be read: {exc}")
    return parse_document(text, name=name)


def parse_document(text: str, *, name: str) -> Any:
    """Parse the text of one raw output by its file name's suffix."""

    lowered = name.lower()
    if lowered.endswith((".csv", ".tsv")):
        delimiter = "\t" if lowered.endswith(".tsv") else ","
        try:
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            rows: list[dict[str, Any]] = []
            for row in reader:
                rows.append(
                    {
                        str(key): _cell(value)
                        for key, value in row.items()
                        if key is not None and value not in (None, "")
                    }
                )
                if len(rows) > MAX_RECORDS:
                    return Unavailable(f"{name} has more than {MAX_RECORDS} rows")
        except csv.Error as exc:
            return Unavailable(f"{name} is not a readable table: {exc}")
        return rows
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:
        return Unavailable(f"{name} could not be read as JSON: {exc}")


def _cell(value: str) -> Any:
    stripped = value.strip()
    try:
        number = float(stripped)
    except ValueError:
        return stripped
    if not math.isfinite(number):
        return stripped
    if number.is_integer() and "." not in stripped and "e" not in stripped.lower():
        return int(number)
    return number


# --------------------------------------------------------------- access --
_MISSING = object()


def _lookup(document: Any, path: str) -> Any:
    """Walk a dotted path. Integer segments index a list."""

    current = document
    if not path:
        return current
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
            continue
        if isinstance(current, Sequence) and not isinstance(current, str | bytes):
            try:
                index = int(segment)
            except ValueError:
                return _MISSING
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
            continue
        return _MISSING
    return current


def _number(value: Any) -> float | None:
    """A finite float, or ``None``. A boolean is not a number."""

    if value is _MISSING or value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) else None


def _hashable(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list | dict):
        return json.dumps(value, sort_keys=True)
    return value


def _holds(condition: Condition, value: Any) -> bool | None:
    """Whether one record's value satisfies one condition.

    ``None`` means the comparison is not defined -- an inequality against
    something that is not a number -- and the record is incomplete rather
    than silently failing the rule.
    """

    if value is _MISSING or value is None:
        return None
    wanted = condition.value
    comparator = condition.comparator
    if comparator in {"<", "<=", ">", ">="}:
        left, right = _number(value), _number(wanted)
        if left is None or right is None:
            return None
        return {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
        }[comparator]
    # Equality is typed. A number equals a number, a boolean a boolean and a
    # string a string; anything else is *undefined*, never "not equal". An
    # independent review found the first version reading a CSV's "False"
    # against a condition `converged == false` as a clean non-match -- so
    # seven failed runs of ten counted as none, and a rule on the failure
    # fraction read SUPPORTS. An undefined comparison makes the record
    # incomplete, which the contract's own policy then decides.
    if isinstance(value, bool) or isinstance(wanted, bool):
        if not (isinstance(value, bool) and isinstance(wanted, bool)):
            return None
        equal = value is wanted
    else:
        left_number, right_number = _number(value), _number(wanted)
        if left_number is not None and right_number is not None:
            equal = left_number == right_number
        elif isinstance(value, str) and isinstance(wanted, str):
            equal = value == wanted
        else:
            return None
    return equal if comparator == "==" else not equal


# --------------------------------------------------------- observables --
def _required_fields(spec: AnalysisSpec) -> dict[str, tuple[set[str], set[str]]]:
    """Per observable: every field a record must carry, and those that must be numbers."""

    required: dict[str, tuple[set[str], set[str]]] = {
        item.name: (set(item.fields), set()) for item in spec.observables
    }
    for item in spec.observables:
        present, numeric = required[item.name]
        for condition in item.include:
            present.add(condition.field)
    for reduction in spec.reductions:
        if reduction.observable not in required:
            continue
        present, numeric = required[reduction.observable]
        present.update(reduction.fields_read())
        # A field compared by an inequality anywhere must be a number in
        # every record -- otherwise the comparison is undefined for it --
        # and so must every field an operation computes with.
        numeric.update(reduction.numeric_fields_read())
    for rule in spec.support:
        if rule.observable in required:
            required[rule.observable][0].update(rule.min_distinct)
    # What the tables read of each observable, and everything that reads a
    # table, resolved to the observable's own fields.
    for name, (fields, numbers) in spec.raw_reads().items():
        if name in required:
            required[name][0].update(fields)
            required[name][1].update(numbers)
    return required


def _selection_conditions(spec: AnalysisSpec, observable: str) -> list[Condition]:
    """Every `where` condition any reduction, or any table's aggregate, applies here."""

    found = [
        condition
        for reduction in spec.reductions
        if reduction.observable == observable
        for condition in reduction.where
    ]
    for table in spec.tables:
        if table.source == observable:
            found.extend(
                condition
                for aggregate in table.aggregates
                for condition in aggregate.where
            )
    return found


def _records(
    observable: Observable,
    document: Any,
    *,
    present: set[str],
    numeric: set[str],
    selections: Sequence[Condition] = (),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filter one records observable. Raises ``_Undefined`` on a hard failure."""

    rows = _lookup(document, observable.path)
    if rows is _MISSING:
        raise _Undefined(
            f"{observable.source} has nothing at {observable.path or '(the top level)'}"
        )
    if not isinstance(rows, list):
        raise _Undefined(
            f"{observable.path or 'the document'} in {observable.source} is not a "
            f"list of records"
        )
    if len(rows) > MAX_RECORDS:
        raise _Undefined(f"{observable.source} holds more than {MAX_RECORDS} records")

    counts = {"total": len(rows), "included": 0, "incomplete": 0, "excluded_by_rule": 0}
    kept: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            counts["incomplete"] += 1
            continue
        values: dict[str, Any] = {}
        complete = True
        for name in sorted(present):
            value = _lookup(row, name)
            if value is _MISSING or value is None:
                complete = False
                break
            if name in numeric and _number(value) is None:
                complete = False
                break
            values[name] = value
        if not complete:
            counts["incomplete"] += 1
            continue
        verdicts = [
            _holds(condition, values[condition.field])
            for condition in observable.include
        ]
        # A record for which some reduction's selection cannot be decided is
        # incomplete for the whole observable, so every reduction sees the
        # same records and a fraction's numerator and denominator agree.
        undecidable = any(
            _holds(condition, values[condition.field]) is None
            for condition in selections
        )
        if any(item is None for item in verdicts) or undecidable:
            counts["incomplete"] += 1
            continue
        if not all(verdicts):
            counts["excluded_by_rule"] += 1
            continue
        kept.append(values)
    counts["included"] = len(kept)
    return kept, counts


# ------------------------------------------------------------ arithmetic --
def _selected(rows: Sequence[Mapping[str, Any]], where: Sequence[Condition]) -> list:
    if not where:
        return list(rows)
    chosen = []
    for row in rows:
        verdicts = [
            _holds(condition, row.get(condition.field, _MISSING)) for condition in where
        ]
        if all(item is True for item in verdicts):
            chosen.append(row)
    return chosen


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between order statistics (Hyndman-Fan type 7)."""

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + weight * (ordered[upper] - ordered[lower])


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. ``None`` when singular."""

    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    scale = max((abs(value) for row in matrix for value in row), default=0.0) or 1.0
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= SINGULAR_TOLERANCE * scale:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / augmented[column][column]
            if factor == 0.0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        total = augmented[row][size] - math.fsum(
            augmented[row][index] * solution[index] for index in range(row + 1, size)
        )
        solution[row] = total / augmented[row][row]
    return solution


def _ols(rows: Sequence[Mapping[str, Any]], reduction: Reduction) -> float:
    terms = list(reduction.terms)
    columns = len(terms) + 1
    if len(rows) <= columns:
        raise _Undefined(
            f"{reduction.name}: a regression with {columns} coefficients needs more "
            f"than {columns} records and has {len(rows)}"
        )
    design: list[list[float]] = []
    response: list[float] = []
    for row in rows:
        entry = [1.0]
        for term in terms:
            product = 1.0
            for part in term.split(":"):
                product *= float(row[part])
            entry.append(product)
        design.append(entry)
        response.append(float(row[reduction.response]))
    normal = [
        [
            math.fsum(design[k][i] * design[k][j] for k in range(len(design)))
            for j in range(columns)
        ]
        for i in range(columns)
    ]
    moment = [
        math.fsum(design[k][i] * response[k] for k in range(len(design)))
        for i in range(columns)
    ]
    solution = _solve(normal, moment)
    if solution is None:
        raise _Undefined(
            f"{reduction.name}: the design does not identify these coefficients -- "
            f"the terms {terms} are collinear or constant in the analysed records"
        )
    return solution[1 + terms.index(reduction.coefficient)]


def _correlation(rows: Sequence[Mapping[str, Any]], reduction: Reduction) -> float:
    if len(rows) < 3:
        raise _Undefined(
            f"{reduction.name}: a correlation needs at least three records"
        )
    xs = [float(row[reduction.field]) for row in rows]
    ys = [float(row[reduction.other_field]) for row in rows]
    mx, my = _mean(xs), _mean(ys)
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        raise _Undefined(f"{reduction.name}: one of the fields does not vary")
    return sxy / math.sqrt(sxx * syy)


def _ranks(values: Sequence[float]) -> list[float]:
    """Fractional ranks from 1: tied values share the mean of the ranks they span."""

    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        shared = (start + end) / 2.0 + 1.0
        for position in range(start, end + 1):
            ranks[order[position]] = shared
        start = end + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """The Pearson correlation, or ``None`` when either variable does not vary."""

    mx, my = _mean(xs), _mean(ys)
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def _rank_correlation(rows: Sequence[Mapping[str, Any]], reduction: Reduction) -> float:
    if len(rows) < 3:
        raise _Undefined(
            f"{reduction.name}: a rank correlation needs at least three records"
        )
    xs = _ranks([float(row[reduction.field]) for row in rows])
    ys = _ranks([float(row[reduction.other_field]) for row in rows])
    value = _pearson(xs, ys)
    if value is None:
        raise _Undefined(f"{reduction.name}: one of the fields does not vary")
    return value


def _term_column(rows: Sequence[Mapping[str, Any]], term: str) -> list[float]:
    column: list[float] = []
    for row in rows:
        product = 1.0
        for part in term.split(":"):
            product *= float(row[part])
        column.append(product)
    return column


def _residuals(
    values: Sequence[float], columns: Sequence[Sequence[float]], reduction: Reduction
) -> list[float]:
    """``values`` less its least-squares fit on an intercept and ``columns``."""

    size = len(columns) + 1
    design = [
        [1.0, *(column[index] for column in columns)] for index in range(len(values))
    ]
    normal = [
        [
            math.fsum(design[k][i] * design[k][j] for k in range(len(design)))
            for j in range(size)
        ]
        for i in range(size)
    ]
    moment = [
        math.fsum(design[k][i] * values[k] for k in range(len(design)))
        for i in range(size)
    ]
    solution = _solve(normal, moment)
    if solution is None:
        raise _Undefined(
            f"{reduction.name}: the terms {list(reduction.terms)} are collinear or "
            f"constant in the analysed records, so they cannot be removed"
        )
    return [
        values[k] - math.fsum(design[k][i] * solution[i] for i in range(size))
        for k in range(len(values))
    ]


def _partial(
    rows: Sequence[Mapping[str, Any]], reduction: Reduction, *, ranked: bool
) -> float:
    """The correlation of two fields once the terms are removed from both.

    Each of ``field`` and ``other_field`` is replaced by its residual from a
    least-squares fit on an intercept and the terms, and the residuals are
    correlated (Pearson). ``ranked`` replaces every variable -- both fields and
    each term's values -- by its fractional ranks first: the partial Spearman
    correlation. A field the terms determine exactly has no residual to
    correlate, and the quantity is undefined rather than whatever rounding
    leaves.
    """

    terms = list(reduction.terms)
    needed = len(terms) + 3
    if len(rows) < needed:
        raise _Undefined(
            f"{reduction.name}: a partial correlation removing {len(terms)} term(s) "
            f"needs at least {needed} records and has {len(rows)}"
        )
    ys = [float(row[reduction.field]) for row in rows]
    xs = [float(row[reduction.other_field]) for row in rows]
    columns = [_term_column(rows, term) for term in terms]
    if ranked:
        ys, xs = _ranks(ys), _ranks(xs)
        columns = [_ranks(column) for column in columns]
    residuals = []
    for name, values in ((reduction.field, ys), (reduction.other_field, xs)):
        left = _residuals(values, columns, reduction)
        centre = _mean(values)
        total = math.fsum((item - centre) ** 2 for item in values)
        if math.fsum(item * item for item in left) <= SINGULAR_TOLERANCE * total:
            raise _Undefined(
                f"{reduction.name}: {name} is determined by the terms "
                f"{terms}, so nothing of it is left to correlate"
            )
        residuals.append(left)
    value = _pearson(residuals[0], residuals[1])
    if value is None:
        raise _Undefined(
            f"{reduction.name}: after removing the terms, a field does not vary"
        )
    return value


def _crossing(rows: Sequence[Mapping[str, Any]], reduction: Reduction) -> float:
    """Where ``other_field`` against ``field`` crosses the level. See ``Crossing``.

    The points are sorted by ``field`` and must have distinct values of it.
    With ``d = other_field - level``, a crossing is a change of strict sign of
    ``d`` between consecutive points where it is not zero: between adjacent
    points it is located by linear interpolation; across points exactly on
    the level, at the middle of them. A point on the level with the same sign
    either side is a touch, not a crossing.
    """

    assert reduction.crossing is not None  # AnalysisSpec.check
    rule = reduction.crossing
    points = sorted(
        (float(row[reduction.field]), float(row[reduction.other_field])) for row in rows
    )
    if len(points) < 2:
        raise _Undefined(
            f"{reduction.name}: a crossing needs a curve of at least two points and "
            f"has {len(points)}"
        )
    for (before, _), (after, _) in pairwise(points):
        if before == after:
            raise _Undefined(
                f"{reduction.name}: two records share {reduction.field} = {before:g}; "
                f"a crossing is of a curve with one {reduction.other_field} per "
                f"{reduction.field}, so aggregate them first"
            )
    offsets = [y - rule.level for _, y in points]
    nonzero = [index for index, offset in enumerate(offsets) if offset != 0.0]
    found: list[tuple[float, str]] = []
    for low, high in pairwise(nonzero):
        if (offsets[low] < 0.0) == (offsets[high] < 0.0):
            continue
        if high == low + 1:
            (x0, y0), (x1, y1) = points[low], points[high]
            location = x0 + (rule.level - y0) * (x1 - x0) / (y1 - y0)
        else:
            location = (points[low + 1][0] + points[high - 1][0]) / 2.0
        found.append((location, "rising" if offsets[low] < 0.0 else "falling"))
    if rule.direction != "any":
        found = [item for item in found if item[1] == rule.direction]
    which = "" if rule.direction == "any" else f" {rule.direction}"
    if not found:
        raise _Undefined(
            f"{reduction.name}: {reduction.other_field} does not cross "
            f"{rule.level:g}{which} along {reduction.field}"
        )
    if rule.pick == "single" and len(found) != 1:
        raise _Undefined(
            f"{reduction.name}: {reduction.other_field} crosses {rule.level:g}{which} "
            f"{len(found)} times, and the contract asked for exactly one"
        )
    location = found[-1][0] if rule.pick == "last" else found[0][0]
    if not math.isfinite(location):
        raise _Undefined(f"{reduction.name}: the crossing is not a finite number")
    return location


def _reduce(
    reduction: Reduction,
    frames: Mapping[str, Any],
    computed: Mapping[str, float | None],
) -> float:
    op = reduction.op
    if op in {"difference", "ratio"}:
        first, second = (computed.get(name) for name in reduction.of)
        if first is None or second is None:
            raise _Undefined(f"{reduction.name}: an operand is undefined")
        if op == "difference":
            return first - second
        if second == 0.0:
            raise _Undefined(f"{reduction.name}: the denominator is zero")
        return first / second
    if op == "expression":
        parsed = expressions.parse(reduction.expression)
        operands = {name: computed.get(name) for name in parsed.variables}
        if any(value is None for value in operands.values()):
            raise _Undefined(f"{reduction.name}: an operand is undefined")
        value = expressions.evaluate(parsed, operands.get)
        if value is None:
            raise _Undefined(
                f"{reduction.name}: {reduction.expression} is undefined for these "
                f"values -- a zero denominator, the logarithm of a number that is "
                f"not positive, or an overflow"
            )
        return value
    frame = frames[reduction.observable]
    if op == "value":
        return float(frame)
    return _over_records(reduction, frame)


def _over_records(reduction: Reduction, frame: Sequence[Mapping[str, Any]]) -> float:
    """One record operation over a frame's records -- or over one group's, in a table."""

    op = reduction.op
    if not frame:
        # A count of nothing is not zero violations. An independent review
        # found "violations == 0" reading SUPPORTS for a run that wrote `[]`,
        # a header-only CSV, or records every one of which was excluded.
        raise _Undefined(
            f"{reduction.name}: {reduction.observable} has no analysed records, so "
            f"nothing was measured"
        )
    rows = _selected(frame, reduction.where)
    if op == "count":
        return float(len(rows))
    if op == "fraction":
        if not frame:
            raise _Undefined(f"{reduction.name}: there are no analysed records")
        return len(rows) / len(frame)
    if op == "correlation":
        return _correlation(rows, reduction)
    if op == "ols_coefficient":
        return _ols(rows, reduction)
    if op == "rank_correlation":
        return _rank_correlation(rows, reduction)
    if op in PARTIAL_OPERATIONS:
        return _partial(rows, reduction, ranked=op == "partial_rank_correlation")
    if op == "crossing":
        return _crossing(rows, reduction)
    values = [float(row[reduction.field]) for row in rows]
    if not values:
        raise _Undefined(
            f"{reduction.name}: no analysed record satisfies its selection"
        )
    if op == "mean":
        return _mean(values)
    if op == "median":
        return _quantile(values, 0.5)
    if op == "quantile":
        assert reduction.q is not None  # AnalysisSpec.check
        return _quantile(values, reduction.q)
    if op == "std":
        if len(values) < 2:
            raise _Undefined(
                f"{reduction.name}: a standard deviation needs two records"
            )
        centre = _mean(values)
        return math.sqrt(
            math.fsum((item - centre) ** 2 for item in values) / (len(values) - 1)
        )
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    if op == "sum":
        return math.fsum(values)
    raise _Undefined(f"{reduction.name}: {op} is not an operation")  # pragma: no cover


def _compute(
    spec: AnalysisSpec,
    frames: Mapping[str, Any],
    *,
    only: set[str] | None = None,
    nested: bool = False,
) -> tuple[dict[str, float | None], list[str]]:
    """Every reduction in order -- or only those in ``only``, a closed chain.

    ``nested`` is a computation inside a resampling, where a permutation null
    is not drawn again (``AnalysisSpec.check`` refuses one in the chain of
    another, and of a bootstrapped statistic).
    """

    computed: dict[str, float | None] = {}
    notes: list[str] = []
    for reduction in spec.reductions:
        if only is not None and reduction.name not in only:
            continue
        try:
            if reduction.op == "permutation_p":
                if nested:
                    raise _Undefined(
                        f"{reduction.name}: a permutation null is not drawn inside "
                        f"another resampling"
                    )
                value = _permutation(spec, reduction, frames, computed)
            else:
                value = _reduce(reduction, frames, computed)
        except (_Undefined, OverflowError, ZeroDivisionError, ValueError) as exc:
            computed[reduction.name] = None
            notes.append(
                str(exc) if isinstance(exc, _Undefined) else f"{reduction.name}: {exc}"
            )
            continue
        computed[reduction.name] = value if math.isfinite(value) else None
        if computed[reduction.name] is None:
            notes.append(f"{reduction.name}: the value is not a finite number")
    return computed, notes


def _chain(spec: AnalysisSpec, name: str) -> set[str]:
    """The reductions the named one depends on, itself included."""

    by_name = {item.name: item for item in spec.reductions}
    found = {name}
    for other in by_name[name].depends_on():
        found |= _chain(spec, other)
    return found


def _dependencies(spec: AnalysisSpec, name: str) -> set[str]:
    """The records observables the named reduction reads, transitively.

    Through a table, the observable its records descend from: resampling an
    observable's records and deriving the table again is how a statistic
    over a table is resampled.
    """

    by_name = {item.name: item for item in spec.reductions}
    reduction = by_name[name]
    if reduction.op in SCALAR_OPERATIONS:
        found: set[str] = set()
        for other in reduction.depends_on():
            found |= _dependencies(spec, other)
        return found
    view = spec.frame_views().get(reduction.observable)
    return {view.observable} if view is not None else set()


# ------------------------------------------------------------------ tables --
def _key_part(value: Any) -> tuple[int, Any]:
    """A value as part of a group key: typed, so ``True`` and ``1`` are two groups."""

    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, int | float):
        return (1, float(value))
    if isinstance(value, str):
        return (2, value)
    return (3, json.dumps(value, sort_keys=True))


def _partition(
    rows: Sequence[Mapping[str, Any]], fields: Sequence[str]
) -> list[list[int]]:
    """The indices of ``rows`` by their values of ``fields``, groups in key order."""

    groups: dict[tuple[tuple[int, Any], ...], list[int]] = {}
    for index, row in enumerate(rows):
        if any(name not in row for name in fields):
            raise _Undefined(
                f"a record lacks one of the fields {list(fields)} it is grouped by"
            )
        key = tuple(_key_part(row[name]) for name in fields)
        groups.setdefault(key, []).append(index)
    return [groups[key] for key in sorted(groups)]


def _table(
    spec: AnalysisSpec, table: Table, frames: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Derive one table from its source frame. Raises ``_Undefined`` on a hard failure."""

    source = frames[table.source]
    made: list[dict[str, Any]] = []
    if table.by:
        for members in _partition(source, table.by):
            group = [source[index] for index in members]
            record: dict[str, Any] = {name: group[0][name] for name in table.by}
            for aggregate in table.aggregates:
                try:
                    value = _over_records(aggregate, group)
                except (_Undefined, OverflowError, ZeroDivisionError, ValueError):
                    continue
                if math.isfinite(value):
                    record[aggregate.name] = value
            made.append(record)
    else:
        made = [dict(row) for row in source]
    for item in table.compute:
        try:
            parsed = expressions.parse(item.expression)
        except expressions.ExpressionError as exc:
            raise _Undefined(f"{item.field}: {exc}") from None
        for record in made:
            value = expressions.evaluate(
                parsed, lambda name, record=record: _number(record.get(name, _MISSING))
            )
            if value is not None:
                record[item.field] = value
    derived = table.derived_fields()
    selections = spec.selections(table.name)
    counts = {
        "total": len(made),
        "included": 0,
        "incomplete": 0,
        "excluded_by_rule": 0,
    }
    kept: list[dict[str, Any]] = []
    for record in made:
        if any(name not in record for name in derived):
            counts["incomplete"] += 1
            continue
        verdicts = [
            _holds(condition, record.get(condition.field, _MISSING))
            for condition in table.include
        ]
        undecidable = any(
            _holds(condition, record.get(condition.field, _MISSING)) is None
            for condition in selections
        )
        if any(item is None for item in verdicts) or undecidable:
            counts["incomplete"] += 1
            continue
        if not all(verdicts):
            counts["excluded_by_rule"] += 1
            continue
        kept.append(record)
    counts["included"] = len(kept)
    return kept, counts


def _rebuilt(
    spec: AnalysisSpec, frames: Mapping[str, Any], tables: Sequence[Table]
) -> dict[str, Any] | None:
    """``frames`` with ``tables`` derived again, in order; ``None`` if one is insufficient."""

    rebuilt = dict(frames)
    for table in tables:
        try:
            rows, counts = _table(spec, table, rebuilt)
        except _Undefined:
            return None
        if counts["incomplete"] and table.incomplete_records == "insufficient":
            return None
        rebuilt[table.name] = rows
    return rebuilt


def _downstream(spec: AnalysisSpec, frame: str) -> list[Table]:
    """The tables derived, directly or not, from ``frame``, in their order."""

    reached = {frame}
    found: list[Table] = []
    for table in spec.tables:
        if table.source in reached:
            reached.add(table.name)
            found.append(table)
    return found


def _bound(spec: AnalysisSpec, frames: Mapping[str, Any], frame: str) -> int:
    """The most records ``frame`` can hold when its observables keep their sizes."""

    tables = {item.name: item for item in spec.tables}
    while frame in tables:
        frame = tables[frame].source
    value = frames.get(frame)
    return len(value) if isinstance(value, list) else 1


def _permutation(
    spec: AnalysisSpec,
    reduction: Reduction,
    frames: Mapping[str, Any],
    computed: Mapping[str, float | None],
) -> float:
    """The permutation p-value of an earlier quantity. See ``contracts.Permutation``.

    ``field`` of the frame's records is shuffled among the records of each
    stratum (Fisher-Yates, from the frozen seed), every table derived from the
    frame is derived again, the statistic is recomputed, and the draws are
    counted against the observed value. A tie counts as at least as extreme;
    so does a draw where the statistic is undefined, and when more than a
    tenth of the draws are undefined the null is not a distribution of the
    statistic and the p-value is undefined. ``(1 + extreme) / (1 + draws)``.
    """

    assert reduction.permutation is not None  # AnalysisSpec.check
    permutation = reduction.permutation
    statistic = reduction.of[0]
    observed = computed.get(statistic)
    if observed is None:
        raise _Undefined(
            f"{reduction.name}: {statistic} is undefined, so there is no observed "
            f"value to set against its null"
        )
    rows = frames.get(reduction.observable)
    if not isinstance(rows, list) or not rows:
        raise _Undefined(
            f"{reduction.name}: {reduction.observable} has no analysed records to "
            f"shuffle"
        )
    downstream = _downstream(spec, reduction.observable)
    chain = _chain(spec, statistic)
    by_name = {item.name: item for item in spec.reductions}
    rederived = {reduction.observable} | {item.name for item in downstream}
    per_draw = len(rows) * (1 + len(downstream)) + sum(
        len(rows)
        if by_name[name].observable in rederived
        else _bound(spec, frames, by_name[name].observable)
        for name in chain
        if by_name[name].observable
    )
    work = permutation.resamples * per_draw
    if work > MAX_RESAMPLING_WORK:
        raise _Undefined(
            f"{reduction.name}: the preregistered permutation null needs about {work} "
            f"record operations and this build computes at most "
            f"{MAX_RESAMPLING_WORK} in one work item"
        )
    strata = _partition(rows, permutation.by)
    values = [row[reduction.field] for row in rows]
    generator = random.Random(permutation.seed)
    tolerance = PERMUTATION_TIE_TOLERANCE * max(1.0, abs(observed))
    at_most = at_least = undefined = 0
    for _ in range(permutation.resamples):
        shuffled = list(values)
        for members in strata:
            for position in range(len(members) - 1, 0, -1):
                other = generator.randrange(position + 1)
                first, second = members[position], members[other]
                shuffled[first], shuffled[second] = shuffled[second], shuffled[first]
        sample = dict(frames)
        sample[reduction.observable] = [
            {**row, reduction.field: shuffled[index]} for index, row in enumerate(rows)
        ]
        rebuilt = _rebuilt(spec, sample, downstream)
        value = None
        if rebuilt is not None:
            statistics, _notes = _compute(spec, rebuilt, only=chain, nested=True)
            value = statistics.get(statistic)
        if value is None:
            undefined += 1
            continue
        if value <= observed + tolerance:
            at_most += 1
        if value >= observed - tolerance:
            at_least += 1
    draws = permutation.resamples
    if undefined > (1.0 - MIN_FINITE_RESAMPLES) * draws:
        raise _Undefined(
            f"{reduction.name}: {statistic} is undefined in {undefined} of {draws} "
            f"permutations, so they are not a null distribution of it"
        )
    lower = (1 + at_most + undefined) / (draws + 1)
    upper = (1 + at_least + undefined) / (draws + 1)
    if permutation.tail == "lower":
        return lower
    if permutation.tail == "upper":
        return upper
    return min(1.0, 2.0 * min(lower, upper))


def _bootstrap(
    spec: AnalysisSpec, frames: Mapping[str, Any]
) -> tuple[tuple[float, float] | None, str]:
    assert spec.uncertainty is not None
    uncertainty = spec.uncertainty
    resampled = [
        item.name
        for item in spec.observables
        if item.name in _dependencies(spec, spec.primary_statistic)
    ]
    # Every table is derived again from each resample, so its source is
    # counted too: at most as many records as the observable it descends from.
    rederived = sum(_bound(spec, frames, item.source) for item in spec.tables)
    draws = uncertainty.resamples * (
        sum(len(frames[name]) for name in resampled) + rederived
    )
    if draws > MAX_BOOTSTRAP_DRAWS:
        return None, (
            f"the preregistered bootstrap needs {draws} record draws and this build "
            f"computes at most {MAX_BOOTSTRAP_DRAWS} in one work item"
        )
    if any(not frames[name] for name in resampled):
        return None, "an observable the bootstrap resamples has no analysed records"
    smallest = min(len(frames[name]) for name in resampled)
    if smallest < MIN_BOOTSTRAP_RECORDS:
        return None, (
            f"a bootstrap over {smallest} record(s) is not an interval; at least "
            f"{MIN_BOOTSTRAP_RECORDS} analysed records are required"
        )
    generator = random.Random(uncertainty.seed)
    chain = _chain(spec, spec.primary_statistic)
    values: list[float] = []
    for _ in range(uncertainty.resamples):
        sample: dict[str, Any] | None = dict(frames)
        assert sample is not None
        for name in resampled:
            rows = frames[name]
            sample[name] = [
                rows[generator.randrange(len(rows))] for _ in range(len(rows))
            ]
        if spec.tables:
            sample = _rebuilt(spec, sample, spec.tables)
            if sample is None:
                continue
        computed, _notes = _compute(spec, sample, only=chain, nested=True)
        value = computed.get(spec.primary_statistic)
        if value is not None:
            values.append(value)
    if len(values) < MIN_FINITE_RESAMPLES * uncertainty.resamples:
        return None, (
            f"only {len(values)} of {uncertainty.resamples} bootstrap resamples gave "
            f"a defined statistic, so no interval is reported"
        )
    if min(values) == max(values) and smallest < MIN_DEGENERATE_RECORDS:
        return None, (
            f"every resample of {smallest} records gave the same value, which "
            f"claims a certainty {smallest} records cannot give"
        )
    tail = (1.0 - uncertainty.level) / 2.0
    return (_quantile(values, tail), _quantile(values, 1.0 - tail)), ""


def _wilson(
    spec: AnalysisSpec, frames: Mapping[str, Any]
) -> tuple[tuple[float, float] | None, str]:
    """The Wilson score interval for a fraction. Closed form, no resampling.

    The right interval for a proportion near 0 or 1, where a percentile
    bootstrap collapses to a point: five of five successes give [0.57, 1.0]
    at 95%, not [1, 1].
    """

    from statistics import NormalDist

    assert spec.uncertainty is not None
    reduction = next(
        item for item in spec.reductions if item.name == spec.primary_statistic
    )
    frame = frames[reduction.observable]
    n = len(frame)
    if n == 0:
        return None, "there are no analysed records to take a fraction of"
    k = len(_selected(frame, reduction.where))
    z = NormalDist().inv_cdf(1.0 - (1.0 - spec.uncertainty.level) / 2.0)
    p_hat = k / n
    denominator = 1.0 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denominator
    half = (z / denominator) * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half)), ""


# -------------------------------------------------------------- evaluate --
def _insufficient(
    summary: str,
    *,
    records: Mapping[str, Mapping[str, int]] | None = None,
    support: Sequence[Mapping[str, Any]] = (),
    statistics: Mapping[str, float | None] | None = None,
    notes: Sequence[str] = (),
) -> AnalysisResult:
    return AnalysisResult(
        conclusion=EmpiricalConclusion.INSUFFICIENT,
        summary=summary,
        records=dict(records or {}),
        support=tuple(support),
        statistics=dict(statistics or {}),
        notes=tuple(notes),
    )


def _described(spec: AnalysisSpec, statistic: float) -> str:
    """The primary statistic and its value, said in terms a reviewer can check.

    A named reduction means nothing to a reader who has not seen the
    contract, so the sentence says what the name is: the number at a path in
    a file, or an operation over a field of an observable.
    """

    reduction = next(
        item for item in spec.reductions if item.name == spec.primary_statistic
    )
    observables = {item.name: item for item in spec.observables}
    if reduction.op == "value":
        observable = observables[reduction.observable]
        return f"{observable.path or '(the document)'} = {statistic:.6g} in {observable.source}"
    if reduction.op in {"difference", "ratio"}:
        what = f"{reduction.op} of {reduction.of[0]} and {reduction.of[1]}"
    elif reduction.op == "ols_coefficient":
        what = (
            f"coefficient of {reduction.coefficient} in {reduction.response} ~ "
            f"{' + '.join(reduction.terms)} over {reduction.observable}"
        )
    elif reduction.op in {"count", "fraction"}:
        what = f"{reduction.op} of {reduction.observable}"
    elif reduction.op == "correlation":
        what = f"correlation of {reduction.field} and {reduction.other_field} over {reduction.observable}"
    elif reduction.op == "rank_correlation":
        what = (
            f"Spearman rank correlation of {reduction.field} and "
            f"{reduction.other_field} over {reduction.observable}"
        )
    elif reduction.op in {"partial_correlation", "partial_rank_correlation"}:
        kind = "Spearman" if reduction.op == "partial_rank_correlation" else "Pearson"
        what = (
            f"partial {kind} correlation of {reduction.field} and "
            f"{reduction.other_field} given {' + '.join(reduction.terms)} over "
            f"{reduction.observable}"
        )
    elif reduction.op == "crossing" and reduction.crossing is not None:
        what = (
            f"{reduction.crossing.pick} crossing of {reduction.other_field} through "
            f"{reduction.crossing.level:g} along {reduction.field} over "
            f"{reduction.observable}"
        )
    elif reduction.op == "expression":
        what = reduction.expression
    elif reduction.op == "permutation_p" and reduction.permutation is not None:
        strata = ", ".join(reduction.permutation.by) or "all records"
        what = (
            f"{reduction.permutation.tail} permutation p-value of {reduction.of[0]}, "
            f"{reduction.field} of {reduction.observable} shuffled within {strata}, "
            f"{reduction.permutation.resamples} permutations, seed "
            f"{reduction.permutation.seed}"
        )
    else:
        what = f"{reduction.op} of {reduction.field} over {reduction.observable}"
    if reduction.where:
        what += " where " + " and ".join(item.rendered() for item in reduction.where)
    return f"{spec.primary_statistic} ({what}) = {statistic:.6g}"


def evaluate(spec: AnalysisSpec, documents: Mapping[str, Any]) -> AnalysisResult:
    """Apply one frozen analysis to the raw outputs a run produced.

    ``documents`` maps each source path the analysis names to the parsed
    document (see :func:`load_document`) or to an :class:`Unavailable`. A
    source absent from the mapping is treated exactly like one that was not
    written.
    """

    if not spec.analysable:
        return _insufficient(
            "no analysis was fixed for this experiment: "
            + (spec.unanalysable_reason or "no reason recorded")
        )
    if spec.language() == 2:
        # Checked when it was frozen, and checked again here: what this module
        # derives from a table or computes from an expression is only defined
        # for an analysis the checker accepts, and a caller that built one
        # some other way does not get a reading of it. (An analysis in the
        # first language is read as it always was; it was frozen under that
        # language's checks and is not re-decided by later ones.)
        try:
            spec.check()
        except (ContractError, ValueError) as exc:
            return _insufficient(
                f"the frozen analysis is not one this engine can read: {exc}"
            )
    required = _required_fields(spec)
    frames: dict[str, Any] = {}
    records: dict[str, dict[str, int]] = {}
    for observable in spec.observables:
        document = documents.get(
            observable.source, Unavailable(f"{observable.source} was not written")
        )
        if isinstance(document, Unavailable):
            return _insufficient(
                f"the raw output {observable.source}, which the preregistered "
                f"analysis reads as {observable.name!r}, is unavailable: "
                f"{document.reason}",
                records=records,
            )
        if observable.kind == "scalar":
            value = _number(_lookup(document, observable.path))
            if value is None:
                return _insufficient(
                    f"{observable.path or 'the document'} in {observable.source} is "
                    f"not a finite number, so {observable.name!r} is undefined",
                    records=records,
                )
            frames[observable.name] = value
            continue
        present, numeric = required[observable.name]
        try:
            rows, counts = _records(
                observable,
                document,
                present=present,
                numeric=numeric,
                selections=_selection_conditions(spec, observable.name),
            )
        except _Undefined as exc:
            return _insufficient(str(exc), records=records)
        records[observable.name] = counts
        if counts["incomplete"] and observable.incomplete_records == "insufficient":
            return _insufficient(
                f"{counts['incomplete']} of {counts['total']} records of "
                f"{observable.name!r} lack a required field or hold a non-number "
                f"where one is required, and the contract fixed that such records "
                f"make the analysis insufficient rather than being dropped",
                records=records,
            )
        frames[observable.name] = rows

    # The second language's tables, in order: records derived from records.
    for table in spec.tables:
        try:
            rows, counts = _table(spec, table, frames)
        except _Undefined as exc:
            return _insufficient(f"table {table.name!r}: {exc}", records=records)
        records[table.name] = counts
        if counts["incomplete"] and table.incomplete_records == "insufficient":
            return _insufficient(
                f"{counts['incomplete']} of {counts['total']} records of table "
                f"{table.name!r} have an aggregate or a computed field that is "
                f"undefined, or a rule that cannot be decided, and the contract "
                f"fixed that such records make the analysis insufficient rather "
                f"than being dropped",
                records=records,
            )
        frames[table.name] = rows

    support: list[dict[str, Any]] = []
    unmet: list[str] = []
    kinds = {item.name: item.kind for item in spec.observables}
    for rule in spec.support:
        frame = frames[rule.observable]
        count = len(frame) if kinds.get(rule.observable, "records") == "records" else 1
        entry: dict[str, Any] = {
            "observable": rule.observable,
            "min_records": rule.min_records,
            "records": count,
            "distinct": {},
            "met": count >= rule.min_records,
        }
        if count < rule.min_records:
            unmet.append(
                f"{rule.observable!r} has {count} analysed record(s) and the "
                f"contract requires {rule.min_records}"
            )
        for name, wanted in sorted(rule.min_distinct.items()):
            distinct = len({_hashable(row[name]) for row in frame})
            entry["distinct"][name] = {"required": wanted, "observed": distinct}
            if distinct < wanted:
                entry["met"] = False
                unmet.append(
                    f"{name!r} takes {distinct} distinct value(s) in the analysed "
                    f"records of {rule.observable!r} and the contract requires {wanted}"
                )
        support.append(entry)
    # And the same requirements over every *selection* the primary statistic
    # is computed on. A support rule stated for an observable is about the
    # data a statistic rests on, and a reduction that computes on a subset
    # rests on the subset: an independent review met a regression over
    # `regime == "hard"` -- three records, two levels -- passing a rule of
    # twelve records and five levels checked over the whole observable, which
    # is the co-design the rule exists to stop moved one filter inward.
    # `count` and `fraction` are exempt: their selection is what is counted.
    rules = {}
    for rule in spec.support:
        rules.setdefault(rule.observable, []).append(rule)
    by_name = {item.name: item for item in spec.reductions}
    for name in sorted(_chain(spec, spec.primary_statistic)):
        reduction = by_name[name]
        if not reduction.where or reduction.op in {"count", "fraction", "value"}:
            continue
        subset = _selected(frames.get(reduction.observable, ()), reduction.where)
        for rule in rules.get(reduction.observable, ()):
            if len(subset) < rule.min_records:
                unmet.append(
                    f"{reduction.name} computes on {len(subset)} record(s) of "
                    f"{rule.observable!r} and the contract requires {rule.min_records}"
                )
            for field_name, wanted in sorted(rule.min_distinct.items()):
                distinct = len({_hashable(row[field_name]) for row in subset})
                if distinct < wanted:
                    unmet.append(
                        f"{reduction.name} computes on records where {field_name!r} "
                        f"takes {distinct} distinct value(s); the contract requires "
                        f"{wanted}"
                    )
    if unmet:
        return _insufficient(
            "the data do not support the preregistered analysis: " + "; ".join(unmet),
            records=records,
            support=support,
        )

    statistics, notes = _compute(spec, frames)
    statistic = statistics.get(spec.primary_statistic)
    if statistic is None:
        why = next(
            (item for item in notes if item.startswith(f"{spec.primary_statistic}:")),
            "; ".join(notes) or "it is undefined for this data",
        )
        return _insufficient(
            f"the primary statistic {spec.primary_statistic!r} is undefined: {why}",
            records=records,
            support=support,
            statistics=statistics,
            notes=notes,
        )

    interval: tuple[float, float] | None = None
    if spec.uncertainty is not None:
        interval, why = (
            _wilson(spec, frames)
            if spec.uncertainty.method == "wilson_score"
            else _bootstrap(spec, frames)
        )
        if interval is None:
            return _insufficient(
                f"{spec.primary_statistic} = {statistic:.6g}, but the "
                f"preregistered uncertainty could not be computed: {why}",
                records=records,
                support=support,
                statistics=statistics,
                notes=notes,
            )

    assert spec.success is not None and spec.failure is not None  # check()
    points = (statistic,) if interval is None else (interval[0], statistic, interval[1])
    supports = [spec.success.holds(point) for point in points]
    contradicts = [spec.failure.holds(point) for point in points]
    if all(supports) and not any(contradicts):
        conclusion = EmpiricalConclusion.SUPPORTS
    elif all(contradicts) and not any(supports):
        conclusion = EmpiricalConclusion.CONTRADICTS
    else:
        conclusion = EmpiricalConclusion.INCONCLUSIVE
        if interval is not None and (any(supports) or any(contradicts)):
            notes.append(
                "the interval straddles a prespecified threshold, so neither "
                "condition holds across it"
            )
        elif any(supports) and any(contradicts):
            notes.append("both prespecified conditions hold for this value")
        else:
            notes.append("neither prespecified condition holds for this value")
    shown = _described(spec, statistic)
    if interval is not None:
        assert spec.uncertainty is not None
        shown += (
            f" ({spec.uncertainty.level:g} Wilson score interval "
            f"[{interval[0]:.6g}, {interval[1]:.6g}])"
            if spec.uncertainty.method == "wilson_score"
            else f" ({spec.uncertainty.level:g} bootstrap interval "
            f"[{interval[0]:.6g}, {interval[1]:.6g}], "
            f"{spec.uncertainty.resamples} resamples, seed {spec.uncertainty.seed})"
        )
    return AnalysisResult(
        conclusion=conclusion,
        summary=(
            f"{shown}; prespecified support {spec.success.rendered()}, "
            f"refutation {spec.failure.rendered()}"
        ),
        statistic=statistic,
        interval=interval,
        statistics=statistics,
        records=records,
        support=tuple(support),
        notes=tuple(notes),
    )


__all__ = [
    "AnalysisResult",
    "Unavailable",
    "evaluate",
    "load_document",
    "parse_bytes",
    "parse_document",
]
