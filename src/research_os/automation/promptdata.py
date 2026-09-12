"""The one boundary where model-originated text becomes prompt data.

Every string a provider produced and a later provider prompt embeds passes
through this module and nowhere else. That is the whole point of it: the
controller's claim is not that each call site remembers to strip a delimiter,
it is that there is a single serializer, so a field that forgets to use it is a
visible omission rather than a silent escape.

Two things are guaranteed here.

**Inertness.** A model-originated string cannot forge the structure of the
prompt that quotes it. Every control character becomes a space - for inline
fields that includes the newline - and every delimiter this module knows about
is replaced wherever it appears. What survives is the semantic text: a finding
that says "delete everything" still says "delete everything" inside the block,
and the downstream worker reads it as a claim rather than as a command.

**A unique boundary.** :func:`render_data_block` assembles the fence itself and
then re-reads what it produced. A block leaves this module only when exactly one
opening and one closing delimiter stand alone on their own lines and no other
fence delimiter appears anywhere in the body. If that does not hold, the run
fails: an ambiguous fence has already lost the distinction it exists to draw.

Nothing here dereferences a path, executes anything, or decides authority. Tool
sets, scopes, acceptance commands, budgets, and run state come from the work
order and from the controller, never from text that passed through here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from research_os.errors import PromptDataError
from research_os.textsafe import CONTROL_CHARS

#: The default cap on one rendered field.
DEFAULT_FIELD_CHARS = 2_000

#: What replaces a delimiter a model wrote into its own output.
REMOVED_DELIMITER = "[removed delimiter]"

#: What marks a field the renderer cut short.
TRUNCATION_MARKER = " [truncated]"

#: Whitespace that survives block rendering, because removing it would corrupt
#: the content rather than protect the prompt: a diff, a traceback, and pytest
#: output are all built out of tabs and newlines.
_KEPT_IN_BLOCKS: frozenset[str] = frozenset({"\n", "\t"})

#: Every character that may not reach a prompt as itself.
#:
#: C0, DEL, and C1, defined once in :mod:`research_os.textsafe` and re-exported
#: here for the callers that read it from this module. A newline or a carriage
#: return forges the line structure a fence is read by; a NUL truncates the
#: value for anything that hands it to a C API; the rest move a terminal cursor
#: or re-colour what a human is reading. The prompt boundary and the display
#: boundary disagree about what to *do* with such a character - this module
#: makes it a space, the display makes it visible - but they must not disagree
#: about which characters they are.


@dataclass(frozen=True, slots=True)
class DataFence:
    """One pair of delimiters that fences model-originated data in a prompt."""

    begin: str
    end: str

    @property
    def delimiters(self) -> tuple[str, str]:
        return (self.begin, self.end)


#: Analyst findings quoted into a downstream work order's prompt.
ANALYST_FENCE = DataFence(
    begin="----- BEGIN ANALYST DATA (UNTRUSTED INPUT) -----",
    end="----- END ANALYST DATA (UNTRUSTED INPUT) -----",
)

#: Reviewer findings quoted into the single bounded repair prompt.
REVIEW_FENCE = DataFence(
    begin="----- BEGIN REVIEWER FINDINGS (ADVISORY DATA) -----",
    end="----- END REVIEWER FINDINGS (ADVISORY DATA) -----",
)

#: The captured output of the acceptance commands the controller ran.
#:
#: Not model output, but not trustworthy either: it is whatever the code a
#: write-enabled worker just wrote chose to print, so it is fenced like any
#: other untrusted text rather than quoted raw.
CHECK_OUTPUT_FENCE = DataFence(
    begin="----- BEGIN CHECK OUTPUT (UNTRUSTED PROGRAM OUTPUT) -----",
    end="----- END CHECK OUTPUT (UNTRUSTED PROGRAM OUTPUT) -----",
)

#: Scholarship retrieved from the internet, quoted to a read-only analyst.
#:
#: Worded differently from the others on purpose. The analyst and reviewer
#: fences say "another worker in this system said this"; this one says "somebody
#: published this on the internet", which is the stronger warning, because its
#: author has never heard of this system and a paper about prompt injection
#: contains prompt injections as its subject matter.
LITERATURE_FENCE = DataFence(
    begin="----- BEGIN RETRIEVED LITERATURE (UNTRUSTED EXTERNAL TEXT) -----",
    end="----- END RETRIEVED LITERATURE (UNTRUSTED EXTERNAL TEXT) -----",
)

#: Every fence the controller generates.
#:
#: One tuple, because :data:`ALL_DELIMITERS` is derived from it and that is what
#: makes every delimiter inert inside every block. A fence defined elsewhere and
#: not listed here would be neutralised in its own block and not in the others,
#: which is precisely the gap this module exists to close.
FENCES: tuple[DataFence, ...] = (
    ANALYST_FENCE,
    REVIEW_FENCE,
    CHECK_OUTPUT_FENCE,
    LITERATURE_FENCE,
)

#: Every delimiter, longest first, so a delimiter that contains another is
#: replaced as a whole rather than left as a fragment.
ALL_DELIMITERS: tuple[str, ...] = tuple(
    sorted(
        {item for fence in FENCES for item in fence.delimiters},
        key=len,
        reverse=True,
    )
)


def prompt_safe(value: str, *, limit: int = DEFAULT_FIELD_CHARS) -> str:
    """Return one model-originated string as a single inert line of prompt data.

    For a field a data block renders on one line: an id, a path, a severity, a
    one-sentence statement. All whitespace is folded to single spaces, so the
    value cannot introduce a line of its own and therefore cannot stand a
    delimiter alone on one.
    """

    flattened = " ".join(_scrub_control(value, keep=frozenset()).split())
    return _clip(_neutralize_delimiters(flattened), limit)


def prompt_safe_block(value: str, *, limit: int = DEFAULT_FIELD_CHARS) -> str:
    """Return model-originated text as inert multi-line prompt data.

    For content whose lines carry meaning - captured command output, a diff, a
    multi-line goal. Newlines and tabs survive because removing them would
    destroy the content; every other control character, and every delimiter,
    does not. The line structure is therefore the content's own, but no line of
    it can be read as the edge of a block.
    """

    text = _scrub_control(value, keep=_KEPT_IN_BLOCKS)
    lines = [_neutralize_delimiters(line.rstrip()) for line in text.split("\n")]
    return _clip("\n".join(lines), limit)


def boundary_count(text: str, delimiter: str) -> int:
    """Return how many effective boundaries ``delimiter`` forms in ``text``.

    Effective means the delimiter stands alone on its own line. That is the
    form the surrounding instructions name and the form a reader - human or
    model - takes as the edge of a block; a delimiter quoted mid-line is text.
    Counted this way rather than as a substring because it is the property that
    actually matters, and the stricter substring count is asserted separately
    when a block is rendered.
    """

    return sum(1 for line in text.splitlines() if line.strip() == delimiter)


def render_data_block(fence: DataFence, body: Sequence[str]) -> str:
    """Render one fenced data block and prove its boundary is unique.

    ``body`` is the already prompt-safe content, one entry per line. The fence
    is added here and nowhere else, and the result is re-read before it is
    returned: exactly one opening and one closing delimiter, each alone on its
    line, and no delimiter of any fence anywhere in the body.

    A failure is a programming error - a field that reached a block without
    passing :func:`prompt_safe` - and it raises rather than returning a prompt
    whose fence cannot be trusted.
    """

    for line in body:
        for delimiter in ALL_DELIMITERS:
            if delimiter in line:
                raise PromptDataError(
                    "a data block body contains the delimiter "
                    f"{delimiter!r}; every model-originated string must be "
                    "rendered through promptdata.prompt_safe before it reaches "
                    "a prompt"
                )
    rendered = "\n".join([fence.begin, *body, fence.end])
    for delimiter, expected in ((fence.begin, 1), (fence.end, 1)):
        found = boundary_count(rendered, delimiter)
        if found != expected:
            raise PromptDataError(
                f"a data block has {found} effective {delimiter!r} boundaries; "
                f"exactly {expected} is the only acceptable number"
            )
    return rendered


def _scrub_control(value: str, *, keep: frozenset[str]) -> str:
    """Return ``value`` with every control character but ``keep`` made a space.

    A space rather than nothing, so removing a newline separates the words it
    separated instead of running them together. What a downstream worker reads
    should be the analyst's sentence, not a new word the sanitizer invented.
    """

    return "".join(
        character if character not in CONTROL_CHARS or character in keep else " "
        for character in value
    )


def _neutralize_delimiters(value: str) -> str:
    """Return ``value`` with every known fence delimiter replaced."""

    for delimiter in ALL_DELIMITERS:
        value = value.replace(delimiter, REMOVED_DELIMITER)
    return value


def _clip(value: str, limit: int) -> str:
    """Return ``value`` truncated to ``limit`` characters, marked when cut."""

    if len(value) <= limit:
        return value
    return value[:limit] + TRUNCATION_MARKER
