# Literature and source intelligence

Reproducible retrieval, caching, deduplication, provenance, and ranking — all
deterministic, all local, all before any model call.

## What this is not

It is **not scientific truth**. A Work is a record about a paper that exists in
the world; a Claim is a statement your project asserts and a human accepted.
They live in different places for that reason:

| | Scientific truth | Literature index |
|---|---|---|
| Where | `.research/` inside one project, Git-tracked | `~/.local/share/research-os/literature/` |
| Shape | Human-readable YAML/Markdown capsule files | One shared SQLite database |
| Scope | One project | Every project on this machine |
| If deleted | You have lost science | You have lost a cache; re-retrieve it |

Nothing in this subsystem can create, modify, or accept a capsule object.

## What runs without a model

Everything except reading a paper and saying what it argues:

- which provider to ask, and how often;
- normalising a DOI, an arXiv id, an OpenAlex id;
- deciding two provider records are the same paper;
- recording which provider supplied which field;
- storing a PDF under the digest of its bytes;
- extracting its text with `pdftotext`;
- BM25 ranking over metadata and full text.

A model is used for exactly one step, read-only, on content that has already
been fenced as untrusted data.

## Providers

Each adapter was verified against the provider's live interface and published
terms, not against a recollection of them.

| Provider | Endpoint | Credential | Rate limit | Notes |
|---|---|---|---|---|
| OpenAlex | `api.openalex.org` | Optional, `RESEARCH_OS_OPENALEX_API_KEY` | 100 req/s ceiling against a daily credit budget | The `mailto` polite pool was **deprecated in February 2026**; the contact address goes in the `User-Agent`. Budget is reported in `X-RateLimit-*` headers and recorded. |
| Crossref | `api.crossref.org` | None | Polite pool: 10 req/s, 3 concurrent | The authoritative retraction signal (`updated-by`). Without a contact address the anonymous pool guarantees no rate, and the probe says so. |
| arXiv | `export.arxiv.org` | None | **1 request per 3 seconds, 1 connection**, counted across every machine you control | The only source here with retrievable full text. |

A key is never a value in configuration; configuration names the environment
variable it is read from. A key is sent as an `Authorization` header, never in a
query string, so it cannot end up in a recorded `request_url` or a shell history.

All adapters share one HTTP client, because each interval is per host: two
clients would each honour arXiv's three seconds separately, which between them
is a breach of the terms.

An unusable provider reports `UNAVAILABLE` rather than raising. "We could not
ask OpenAlex today" is a fact about a literature review that has to survive into
the record.

## Pacing that outlives the process

The HTTP client paces one process: each host has a minimum interval and the
client waits. What it could not do was remember. Two runs started an hour apart
each began believing every provider was fresh, and a provider that answered the
first with `429 Retry-After: 3600` answered the second the same way, for the
same reason, at the same cost.

Four rules now, over one table in the literature store.

**Cache first, before the slot rather than after it.** A query this store
already answered successfully inside the freshness window is answered from the
store, for no provider quota at all. Only a successful search is served back: a
provider that failed last time has not answered this query, and serving that as
a cache hit would turn one outage into a permanent empty result. The window is
`cache_ttl_seconds`, a day by default; set it to `0` to ask every time, which is
what a reproducibility check wants.

**The reservation is atomic.** Deciding a slot is free and taking it happen
inside one `BEGIN IMMEDIATE` transaction, so two concurrent runs cannot both see
"available" and both issue a request. The network call happens after that
transaction commits — a write lock is never held across I/O, because that would
turn one slow provider into a stalled literature subsystem for every other
process on the machine.

**Only what a provider actually said is recorded.** `Retry-After` in both forms
RFC 9110 allows, a 429, a timeout, a permanent error. A 429 with no
`Retry-After` is held for that source's own minimum interval and nothing longer:
inventing a cooldown a provider did not ask for would be this client deciding,
on no evidence, that a literature review should stop. A quota nobody reported
stays unknown, and unknown is a value. A failure is not a rate limit — a
provider that timed out has not asked us to wait.

**Nothing is waited out irrationally.** A provider asking for an hour gets an
hour recorded, not slept through. The response comes straight back, the source
is marked `RATE_LIMITED` with the time it may be asked again, and the rest of
the retrieval asks the providers that will answer.

This is operational state, not scientific state. Deleting it costs a run some
politeness and no science at all. `researchctl lit sources` shows it beside the
probe: the probe says what this machine *could* do with a provider, the
persisted health says what that provider last actually did.

## Identity and deduplication

A precedence, not a similarity score:

1. DOI
2. arXiv id (version suffix dropped — `v1` and `v3` are one work at two moments)
3. OpenAlex id
4. Otherwise: normalised title **and** first-author surname **and** year

The fallback is deliberately strict. Same-titled papers by different groups in
different years are ordinary, and a false merge silently destroys a distinct
work — which is worse than a duplicate a human can see.

Titles normalise past case, punctuation, accents, and whitespace, and the word
pattern covers non-Latin scripts: reducing a Chinese title to the Latin words it
happens to contain would make two unrelated papers share one fallback identity.

When a later record proves two rows were always one work — typically a Crossref
record supplying the DOI for something arXiv had only as a preprint — they are
merged. Every identifier, payload, file, document, and chunk moves to the
winner, and the losing key becomes an alias, so an id you wrote down last week
still resolves.

## Provenance

Nothing a provider said is ever destroyed.

- `source_records` keeps each provider payload verbatim, with its digest and the
  exact request URL.
- `field_provenance` records which provider asserted which field and when, and
  whether that value is the one in force or was superseded.

An existing field is not overwritten by a later provider; the competing value is
recorded instead. `researchctl lit show` prints both.

## Search

SQLite FTS5 with BM25, weighted so a title match outranks an abstract match,
outranks an author or venue match. Ties break on the work key, so the same index
and query produce the same order every time.

Both indexes stem with Porter. Without it, "widget deformation under load" would
not match "we measure how widgets deform when loaded" — the same paper, written
the way people write abstracts.

A query is searched for, never executed as FTS5 syntax: every token is extracted
and re-quoted as a literal, so `AND`, `NEAR`, `*`, a column filter, or a stray
quote is a word rather than an operator.

If no work contains every term, the search relaxes to any-term **once** and says
so in each result's `matched` field. A widened answer is never presented as the
one that was asked for.

No embeddings, no vector database. They would add a dependency, a build step, a
similarity threshold nobody can justify, and a second copy of the corpus that
goes stale — in exchange for recall this subsystem has no evidence it needs. The
known-item evaluation exists so that claim can be checked rather than assumed.

## Full text

Only what a provider advertised as openly available is fetched. Research OS does
not go looking for a copy of a paywalled paper.

Files are content-addressed under `<data home>/literature/files/`. A response
that claims to be a PDF but does not begin like one is refused — that is a login
wall, and indexing its text would put a publisher's cookie notice into the
literature record.

Text extraction is `pdftotext` (poppler), run as an argument vector with a
timeout. No OCR: a scan with no text layer is reported `unsupported` rather than
guessed at, because a silently empty document makes "this paper does not mention
X" and "we could not read this paper" the same answer.

## The untrusted-content boundary

Retrieved scholarship is text a stranger wrote and published. A paper about
prompt injection contains prompt injections as its subject matter.

So retrieved content may reach exactly one kind of worker:

```
provider → store (deterministic) → local index → fenced packet → literature analyst
                                                                        ↓
                                                          structured findings (DATA)
                                                                        ↓
                                        coder / experimenter / paper writer / human
```

The literature analyst has **no tools at all** — not even the read-only file
tools the repository analyst gets — because everything it needs is in its prompt.
A worker with nothing to act with cannot act on an instruction hidden in an
abstract.

Its output is validated with one rule that does the real work: **every work it
cites must have been in the packet it was given.** A key that was not supplied
invalidates the whole report. That is the mechanism behind "no fabricated
citations" downstream — a paper writer never sees source text, only these
findings, and their keys were checked here.

A retracted work is labelled in the packet, loudly, rather than hidden: it is
still evidence of what was once believed, and the analyst is told it may not be
used to support a finding without saying so.

## Known-item evaluation

A regression guard, not a benchmark. For a query where you have already decided
which works are relevant, does the ranking still return them near the top?

```bash
researchctl lit known nv --query "nitrogen vacancy diamond thermometry" \
    --work doi:10.1063/1.5037053 --note "canonical measurement paper"
researchctl lit eval nv
```

It exits non-zero when a known item stops being retrieved. Do not tune a fixture
until it passes: a fixture optimised against has stopped measuring anything.

A fixture written before a merge keeps working — expected keys are resolved
through the alias table.

## Commands

```bash
researchctl lit sources                     # what this machine can use, and why not
researchctl lit retrieve "QUERY" [--limit N]  # ask every enabled provider, index results
researchctl lit fetch IDENTIFIER [--fulltext] # one work by DOI or arXiv id
researchctl lit search "QUERY" [--json]     # local index only; makes no network request
researchctl lit show WORK_KEY               # one work, with full field provenance
researchctl lit index [--extract]           # rebuild the index
researchctl lit known FIXTURE ...           # record or list a known-item fixture
researchctl lit eval FIXTURE                # run a fixture; non-zero on regression
researchctl lit status                      # where everything lives
```

## Configuration

`~/.config/research-os/literature.yaml`, every key optional:

```yaml
contact_email: you@example.edu     # providers ask for one; Crossref rewards it
enabled_sources: [openalex, crossref, arxiv]
offline: false                     # true reaches nothing and says so
default_search_limit: 20
fetch_fulltext: true
cache_ttl_seconds: 86400           # 0 asks the providers every time
```

A contact address may also come from `RESEARCH_OS_CONTACT_EMAIL`. Credentials
never appear here.

## Display safety

Every renderer returns through `research_os.textsafe.terminal_safe`, so a title
containing an ANSI escape is shown rather than executed. The stored record keeps
the original bytes.
