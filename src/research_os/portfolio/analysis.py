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
from pathlib import Path
from typing import Any

from research_os.portfolio.contracts import (
    FIELD_OPERATIONS,
    AnalysisSpec,
    Condition,
    Observable,
    Reduction,
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

#: The evaluator's own identity, recorded with every reading. The digests
#: bind the *specification*; this binds the semantics that read it, so a
#: later change to how an analysis is evaluated is visible beside every
#: conclusion reached before it.
ENGINE_VERSION = "portfolio.analysis@1"


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
        # every record -- otherwise the comparison is undefined for it.
        numeric.update(
            condition.field
            for condition in reduction.where
            if condition.comparator in {"<", "<=", ">", ">="}
        )
        if reduction.op in FIELD_OPERATIONS or reduction.op == "correlation":
            numeric.update(
                item for item in (reduction.field, reduction.other_field) if item
            )
        if reduction.op == "ols_coefficient":
            numeric.add(reduction.response)
            for term in reduction.terms:
                numeric.update(term.split(":"))
    for rule in spec.support:
        if rule.observable in required:
            required[rule.observable][0].update(rule.min_distinct)
    return required


def _selection_conditions(spec: AnalysisSpec, observable: str) -> list[Condition]:
    """Every `where` condition any reduction applies to this observable."""

    return [
        condition
        for reduction in spec.reductions
        if reduction.observable == observable
        for condition in reduction.where
    ]


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
    frame = frames[reduction.observable]
    if op == "value":
        return float(frame)
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
    spec: AnalysisSpec, frames: Mapping[str, Any]
) -> tuple[dict[str, float | None], list[str]]:
    computed: dict[str, float | None] = {}
    notes: list[str] = []
    for reduction in spec.reductions:
        try:
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
    for other in by_name[name].of:
        found |= _chain(spec, other)
    return found


def _dependencies(spec: AnalysisSpec, name: str) -> set[str]:
    """The records observables the named reduction reads, transitively."""

    by_name = {item.name: item for item in spec.reductions}
    kinds = {item.name: item.kind for item in spec.observables}
    reduction = by_name[name]
    if reduction.op in {"difference", "ratio"}:
        found: set[str] = set()
        for other in reduction.of:
            found |= _dependencies(spec, other)
        return found
    return (
        {reduction.observable}
        if kinds.get(reduction.observable) == "records"
        else set()
    )


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
    draws = uncertainty.resamples * sum(len(frames[name]) for name in resampled)
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
    values: list[float] = []
    for _ in range(uncertainty.resamples):
        sample = dict(frames)
        for name in resampled:
            rows = frames[name]
            sample[name] = [
                rows[generator.randrange(len(rows))] for _ in range(len(rows))
            ]
        computed, _notes = _compute(spec, sample)
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

    support: list[dict[str, Any]] = []
    unmet: list[str] = []
    kinds = {item.name: item.kind for item in spec.observables}
    for rule in spec.support:
        frame = frames[rule.observable]
        count = len(frame) if kinds[rule.observable] == "records" else 1
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
