"""The one place Research OS reaches the network, and the only shape it may take.

Deliberately the standard library. ``urllib.request`` is enough for three JSON
and Atom endpoints, and adding an HTTP stack for them would be a dependency the
rest of the system does not need and a reviewer would have to audit.

Everything about a request is decided here rather than by a caller:

* **Politeness is enforced, not requested.** Each host has a minimum interval
  between requests, taken from that provider's published terms, and the client
  sleeps to honour it. arXiv asks for one request every three seconds from all
  machines under your control; a client that merely documented that would break
  it the first time two adapters ran in one process.
* **A response is bounded.** A body is read up to a cap and then abandoned. A
  provider that streams forever, or a PDF that is actually a zip bomb, costs a
  bounded amount of memory and a bounded amount of time.
* **Credentials do not travel.** An ``Authorization`` header is bound to the
  host it was configured for, and a redirect to a different host drops it. A
  redirect that changes scheme is refused outright.

What comes back is **untrusted data**. Nothing in this module parses content
into anything but bytes and headers, and nothing downstream of it may hand that
content to a model with tools. That boundary is in
:mod:`research_os.literature.packet`; this module's job is to make sure the
bytes arrived honestly and are recorded.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import IO

from research_os import __version__
from research_os.errors import SourceUnavailableError

#: How much of one response body is ever read into memory.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: How long one request may take before it is abandoned.
DEFAULT_TIMEOUT_SECONDS = 30

#: How many times a request is retried after a throttling or server error.
#:
#: Small on purpose. A provider that is rate limiting us is telling us to stop,
#: and a retry loop that outlasts its patience is how an IP gets blocked.
MAX_RETRIES = 2

#: The longest a ``Retry-After`` is honoured before the request is abandoned.
MAX_RETRY_AFTER_SECONDS = 60

#: Statuses worth trying again. Everything else is the provider's answer.
RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def user_agent(contact_email: str | None) -> str:
    """Return the User-Agent every request carries.

    Providers ask to be able to identify and contact a client, and Crossref
    grants its polite pool on exactly that basis. When the researcher has
    configured a contact address it is included; when they have not, the client
    still identifies itself rather than pretending to be a browser.
    """

    base = f"research-os/{__version__} (+https://github.com/nicacevedo/research-os)"
    return f"{base} mailto:{contact_email}" if contact_email else base


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One complete response, with the headers a rate limiter needs."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool = False

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass
class HostPolicy:
    """How often one host may be asked, and what it is called.

    ``min_interval_seconds`` comes from the provider's published terms, not from
    a guess. The one place it is allowed to be wrong is too slow.
    """

    host: str
    min_interval_seconds: float
    note: str = ""


@dataclass
class HttpClient:
    """A polite, bounded, credential-scoped HTTP client."""

    contact_email: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_bytes: int = MAX_RESPONSE_BYTES
    policies: dict[str, HostPolicy] = field(default_factory=dict)
    offline: bool = False
    _last_request: dict[str, float] = field(default_factory=dict, repr=False)

    def policy_for(self, host: str) -> HostPolicy:
        return self.policies.get(host, HostPolicy(host=host, min_interval_seconds=1.0))

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        credential_host: str | None = None,
    ) -> HttpResponse:
        """Fetch one URL, waiting first if this host was asked too recently.

        ``credential_host`` names the host any ``Authorization`` header belongs
        to. A redirect away from it drops the header, so a provider that
        redirects to a CDN cannot be handed a key configured for its API.
        """

        if self.offline:
            raise SourceUnavailableError(
                "network access is disabled for this Research OS client "
                "(offline mode); no literature provider can be reached"
            )
        supplied = dict(headers or {})
        supplied.setdefault("User-Agent", user_agent(self.contact_email))
        supplied.setdefault("Accept-Encoding", "identity")
        attempt = 0
        current = url
        while True:
            parsed = self._validated(current)
            request_headers = dict(supplied)
            if credential_host and parsed.hostname != credential_host:
                request_headers.pop("Authorization", None)
            self._wait_for(parsed.hostname or "")
            response = self._perform(current, request_headers)
            if response.status in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                attempt += 1
                self._sleep(self._retry_delay(response, attempt))
                continue
            return response

    # -- internals -------------------------------------------------------

    def _validated(self, url: str) -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise SourceUnavailableError(
                f"refusing to fetch {url!r}: literature retrieval is HTTPS only, "
                "so metadata and full text cannot be read or altered in transit"
            )
        if not parsed.hostname:
            raise SourceUnavailableError(f"refusing to fetch {url!r}: no host")
        return parsed

    def _wait_for(self, host: str) -> None:
        policy = self.policy_for(host)
        last = self._last_request.get(host)
        now = time.monotonic()
        if last is not None:
            remaining = policy.min_interval_seconds - (now - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = time.monotonic()

    @staticmethod
    def _sleep(seconds: float) -> None:
        """Wait. Its own method so a test can make politeness instantaneous."""

        if seconds > 0:
            time.sleep(seconds)

    def _retry_delay(self, response: HttpResponse, attempt: int) -> float:
        raw = response.header("retry-after")
        if raw and raw.strip().isdigit():
            return min(float(raw.strip()), MAX_RETRY_AFTER_SECONDS)
        return min(float(2**attempt), MAX_RETRY_AFTER_SECONDS)

    def _perform(self, url: str, headers: dict[str, str]) -> HttpResponse:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            # The scheme was checked above: this only ever opens HTTPS.
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as handle:
                body, truncated = read_bounded(handle, self.max_bytes)
                return HttpResponse(
                    url=handle.geturl(),
                    status=handle.status,
                    headers={
                        key.lower(): value for key, value in handle.headers.items()
                    },
                    body=body,
                    truncated=truncated,
                )
        except urllib.error.HTTPError as exc:
            # An error response is still a response: its status and headers are
            # how a provider says "slow down" or "that DOI does not exist", and
            # discarding them would turn both into the same opaque failure.
            body = b""
            try:
                body = exc.read(self.max_bytes)
            except OSError:
                pass
            return HttpResponse(
                url=url,
                status=exc.code,
                headers={
                    key.lower(): value for key, value in (exc.headers or {}).items()
                },
                body=body,
            )
        except urllib.error.URLError as exc:
            raise SourceUnavailableError(
                f"cannot reach {urllib.parse.urlparse(url).hostname}: {exc.reason}"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise SourceUnavailableError(f"request to {url} failed: {exc}") from exc


def read_bounded(stream: IO[bytes], max_bytes: int) -> tuple[bytes, bool]:
    """Read at most ``max_bytes`` from ``stream``, saying whether more remained.

    One byte past the cap is requested and then discarded. That is what makes
    "this response was cut short" distinguishable from "this response happened to
    be exactly the size of the cap", which matters because a truncated PDF is not
    a PDF and must not be stored as one.

    Its own function so the bound can be tested against a stream rather than
    against a live server: a cap that only ever runs inside a socket call is a
    cap nobody has watched work.
    """

    body = stream.read(max_bytes + 1)
    if len(body) > max_bytes:
        return body[:max_bytes], True
    return body, False


def encode_query(parameters: dict[str, str | int | None]) -> str:
    """Return a deterministic query string, dropping unset parameters.

    Sorted so the same logical request produces the same URL every time, which
    is what makes a recorded ``request_url`` comparable across runs.
    """

    usable = {
        key: str(value)
        for key, value in sorted(parameters.items())
        if value is not None
    }
    return urllib.parse.urlencode(usable, quote_via=urllib.parse.quote)
