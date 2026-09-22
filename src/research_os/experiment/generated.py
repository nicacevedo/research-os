"""Documents a plan may compose, and everything that bounds one.

The one parameter kind whose value is *content* rather than a choice among
what the repository already holds, and the reason it exists is a measured
bottleneck rather than a convenience.

A ``path`` parameter may only name a **tracked** file. That is the right rule
and it had a consequence nobody designed: a genuinely new experimental design
inside an already-approved capability required a person to author and commit
a plan file. On 2026-09-22 a real portfolio was blocked on exactly that --
``sweep-lambda-support`` could execute the ``(n, p) x difficulty`` question
it was asked, and what was missing was not an executable capability but one
committed JSON document. A human was operating ordinary research
progression, which is the thing this system exists not to require.

What moves and what does not:

```text
the researcher declares the capability, the parameter, its schema, its size
        v
a plan composes one document inside that declaration
        v
Research OS canonicalises it, checks it, hashes it, and chooses where it lands
        v
the bytes are frozen before anything runs and are what the preregistration binds
```

The caller never supplies a destination. It cannot name a path, escape a
worktree, or decide what runs -- those remain properties of
``experiments.yaml``, which lives outside every worktree. What it gains is
the ability to say *which measurement*, inside a family a person approved.

**The schema subset is closed, and unknown keywords are refused.** A
researcher who writes ``pattern:`` and is silently not given it has been told
a constraint is enforced when it is not -- the same defect this codebase has
now paid for five times in the other direction, where a limit was enforced
and never stated. So the checker refuses a schema it cannot honour, at
configuration time, rather than honouring part of one.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_os.errors import ExperimentSpecError

#: Where a frozen document is written inside the disposable workspace.
#:
#: Research OS's own reserved namespace, and chosen by Research OS: the whole
#: point is that the caller does not get to say where its bytes land.
GENERATED_INPUT_DIR = ".research-os/experiment-inputs"

#: How deep a generated document, or the schema describing one, may nest.
MAX_DOCUMENT_DEPTH = 12

#: The JSON Schema keywords this checker honours. Anything else is refused
#: rather than ignored.
SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "description",
        "title",
    }
)

#: The values ``type`` may take.
SCHEMA_TYPES: frozenset[str] = frozenset(
    {"object", "array", "string", "number", "integer", "boolean"}
)


@dataclass(frozen=True, slots=True)
class GeneratedInput:
    """One frozen document, and where Research OS decided to put it."""

    parameter: str
    #: Repository-relative, chosen here, never supplied.
    path: str
    canonical: bytes
    sha256: str

    @property
    def byte_size(self) -> int:
        return len(self.canonical)

    def record(self) -> dict[str, Any]:
        """What the preregistration stores about it.

        The byte digest and the path, and deliberately not the content: the
        content is reconstructible from the artifact store by that digest,
        and a preregistration that inlined it would grow without bound.
        """

        return {
            "parameter": self.parameter,
            "path": self.path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }


def assert_schema_supported(schema: Mapping[str, Any], *, where: str) -> None:
    """Refuse a schema this checker cannot honour in full.

    Called when the *declaration* is parsed, so a researcher learns at
    configuration time that a keyword does nothing here, rather than
    believing a constraint holds because nothing complained.
    """

    _assert_schema(schema, where=where, depth=0)


def _assert_schema(schema: Any, *, where: str, depth: int) -> None:
    if depth > MAX_DOCUMENT_DEPTH:
        raise ValueError(f"{where}: schema nests deeper than {MAX_DOCUMENT_DEPTH}")
    if not isinstance(schema, Mapping):
        # `ValueError` rather than `TypeError` throughout this function, for
        # the reason `spec._assert_relative` gives: it runs inside a pydantic
        # validator, which turns a `ValueError` into a field error and lets a
        # `TypeError` escape as itself.
        raise ValueError(f"{where}: every schema must be an object")  # noqa: TRY004
    unknown = sorted(set(map(str, schema)) - SCHEMA_KEYWORDS)
    if unknown:
        raise ValueError(
            f"{where}: this checker does not honour {', '.join(unknown)}. "
            f"Supported: {', '.join(sorted(SCHEMA_KEYWORDS))}. A keyword that "
            f"is accepted and not enforced is worse than one that is refused"
        )
    declared = schema.get("type")
    if declared is not None and str(declared) not in SCHEMA_TYPES:
        raise ValueError(
            f"{where}: type {declared!r} is not one of "
            f"{', '.join(sorted(SCHEMA_TYPES))}"
        )
    extra = schema.get("additionalProperties")
    if extra is not None and extra is not False:
        raise ValueError(
            f"{where}: additionalProperties must be false where it appears. A "
            f"generated document is composed by a model for a program the "
            f"researcher trusts, and an unlisted key is what should not pass"
        )
    for name, child in dict(schema.get("properties") or {}).items():
        _assert_schema(child, where=f"{where}.{name}", depth=depth + 1)
    if "items" in schema:
        _assert_schema(schema["items"], where=f"{where}[]", depth=depth + 1)


def canonical_bytes(document: Any, *, where: str) -> bytes:
    """The exact bytes that will be written, hashed and preregistered.

    Sorted keys and compact separators, so a document that differs only in
    key order or whitespace is the same document and hashes alike. Non-finite
    numbers are refused rather than serialised: ``NaN`` is not valid JSON, it
    compares false against every bound a researcher wrote, and this codebase
    has already had one ``NaN`` reach a conclusion.
    """

    try:
        text = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExperimentSpecError(
            f"{where} is not a JSON document this can freeze: {exc}"
        ) from None
    return text.encode("utf-8")


def validate_document(document: Any, schema: Mapping[str, Any], *, where: str) -> None:
    """Check one composed document against the researcher's declared schema."""

    _check(document, schema, where=where, depth=0)


def _check(value: Any, schema: Mapping[str, Any], *, where: str, depth: int) -> None:
    if depth > MAX_DOCUMENT_DEPTH:
        raise ExperimentSpecError(f"{where} nests deeper than {MAX_DOCUMENT_DEPTH}")

    if "const" in schema and value != schema["const"]:
        raise ExperimentSpecError(f"{where} must be {schema['const']!r}")
    if "enum" in schema:
        allowed = list(schema["enum"])
        if value not in allowed:
            raise ExperimentSpecError(
                f"{where} must be one of "
                f"{', '.join(repr(item) for item in allowed)}, not {value!r}"
            )

    declared = schema.get("type")
    if declared is not None and not _is_type(value, str(declared)):
        raise ExperimentSpecError(f"{where} must be a {declared}, not {value!r}")

    if isinstance(value, float) and not math.isfinite(value):
        raise ExperimentSpecError(f"{where} must be a finite number, not {value!r}")

    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and value < low:
            raise ExperimentSpecError(f"{where} must be at least {low}, not {value}")
        if high is not None and value > high:
            raise ExperimentSpecError(f"{where} must be at most {high}, not {value}")
        return
    if isinstance(value, str):
        low, high = schema.get("minLength"), schema.get("maxLength")
        if low is not None and len(value) < low:
            raise ExperimentSpecError(f"{where} must be at least {low} characters")
        if high is not None and len(value) > high:
            raise ExperimentSpecError(f"{where} must be at most {high} characters")
        return
    if isinstance(value, Mapping):
        _check_object(value, schema, where=where, depth=depth)
        return
    if isinstance(value, Sequence):
        _check_array(value, schema, where=where, depth=depth)
        return
    if value is None:
        raise ExperimentSpecError(f"{where} must not be null")
    raise ExperimentSpecError(f"{where} is not a JSON value: {value!r}")


def _check_object(
    value: Mapping[str, Any], schema: Mapping[str, Any], *, where: str, depth: int
) -> None:
    properties = dict(schema.get("properties") or {})
    for name in schema.get("required") or ():
        if str(name) not in value:
            raise ExperimentSpecError(f"{where} is missing required {name!r}")
    if properties:
        # Closed by default, which is the opposite of JSON Schema's default
        # and is deliberate: this document reaches a program the researcher
        # trusts, and a key nobody declared is the one that should not.
        unknown = sorted(set(map(str, value)) - set(map(str, properties)))
        if unknown:
            raise ExperimentSpecError(
                f"{where} has undeclared key(s) {', '.join(unknown)}; the "
                f"declaration lists {', '.join(sorted(properties)) or '(none)'}"
            )
    for name, child in value.items():
        sub = properties.get(str(name))
        if sub is not None:
            _check(child, sub, where=f"{where}.{name}", depth=depth + 1)


def _check_array(
    value: Sequence[Any], schema: Mapping[str, Any], *, where: str, depth: int
) -> None:
    low, high = schema.get("minItems"), schema.get("maxItems")
    if low is not None and len(value) < low:
        raise ExperimentSpecError(f"{where} must have at least {low} item(s)")
    if high is not None and len(value) > high:
        raise ExperimentSpecError(f"{where} must have at most {high} item(s)")
    item_schema = schema.get("items")
    if item_schema is None:
        return
    for index, item in enumerate(value):
        _check(item, item_schema, where=f"{where}[{index}]", depth=depth + 1)


def _is_type(value: Any, declared: str) -> bool:
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if declared == "string":
        return isinstance(value, str)
    if declared == "object":
        return isinstance(value, Mapping)
    if declared == "array":
        return isinstance(value, Sequence) and not isinstance(value, str | bytes)
    return False  # pragma: no cover - `assert_schema_supported` refuses these


def freeze(
    *,
    parameter: str,
    document: Any,
    schema: Mapping[str, Any],
    max_bytes: int,
    command: str,
) -> GeneratedInput:
    """Validate, canonicalise, bound and hash one composed document.

    Order matters. The schema is checked against the *parsed* document, the
    ceiling against the *canonical bytes*, and the digest is of exactly the
    bytes that will be written -- so what the preregistration binds, what the
    workspace receives and what the artifact store holds are one string of
    bytes with one name.
    """

    where = f"command {command!r} parameter {parameter!r}"
    validate_document(document, schema, where=where)
    canonical = canonical_bytes(document, where=where)
    if len(canonical) > max_bytes:
        raise ExperimentSpecError(
            f"{where} composed {len(canonical)} bytes and the declaration "
            f"allows {max_bytes}"
        )
    digest = hashlib.sha256(canonical).hexdigest()
    return GeneratedInput(
        parameter=parameter,
        path=f"{GENERATED_INPUT_DIR}/{parameter}-{digest[:16]}.json",
        canonical=canonical,
        sha256=digest,
    )
