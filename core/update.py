# -*- coding: utf-8 -*-
"""Update detection: "is there a newer acggr than the one I am running".

Data sources, in priority order (all read-only, no shell, no self-replace):
1. fixed feed — update.json on the permanent updater release tag (address never changes,
   drafts/prereleases cannot hide it, rollback is one file upload).
2. GitHub Releases API — fallback when the feed is missing/unreachable; prereleases filtered.
3. None — offline/broken network is normal and stays silent.

Only the version decision lives here; nothing touches Qt / config store / network lifecycle.
The ACGG_UPDATE_FEED / ACGG_UPDATE_API / ACGG_NO_UPDATE_CHECK hatches live here too. Upgrading is
not our business — the UI only points at the release page.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from loguru import logger

from core.variables import __version__ as _CURRENT_VERSION

__all__ = [
    "UPDATE_FEED_URL",
    "GITHUB_RELEASES_URL",
    "UpdateInfo",
    "ProbeOutcome",
    "parse_version",
    "compare_versions",
    "is_prerelease",
    "is_update_available",
    "probe_updates",
    "check_for_update",
    "parse_feed",
    "parse_releases",
    "update_check_enabled",
]

GITHUB_REPO = "jasoneri/acggr"
#: Permanent release tag holding the feed, next to the ``preset`` asset channel.
UPDATE_FEED_TAG = "updater"
UPDATE_FEED_URL = f"https://github.com/{GITHUB_REPO}/releases/download/{UPDATE_FEED_TAG}/update.json"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
RELEASE_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"

DEFAULT_TIMEOUT = 8.0
#: GitHub rejects requests without a User-Agent.
_UA = f"acggr/{_CURRENT_VERSION} (+{GITHUB_REPO})"

#: Documented escape hatches, not shipped defaults.
#: - ``ACGG_UPDATE_FEED`` / ``ACGG_UPDATE_API``: point the probe at a mirror or a
#:   CDN (or a test double) without a rebuild. Both are plain URLs.
#: - ``ACGG_NO_UPDATE_CHECK=1``: disable the probe entirely (CI, privacy, or a
#:   user behind a metered link who does not want a startup request).
FEED_URL_ENV = "ACGG_UPDATE_FEED"
API_URL_ENV = "ACGG_UPDATE_API"
DISABLE_ENV = "ACGG_NO_UPDATE_CHECK"


def update_check_enabled() -> bool:
    return str(os.environ.get(DISABLE_ENV, "")).strip().lower() not in {"1", "true", "yes", "on"}


def _feed_url() -> str:
    return str(os.environ.get(FEED_URL_ENV, "")).strip() or UPDATE_FEED_URL


def _releases_url() -> str:
    return str(os.environ.get(API_URL_ENV, "")).strip() or GITHUB_RELEASES_URL

_PRE_ORDER = {
    "a": 0, "alpha": 0,
    "b": 1, "beta": 1,
    "c": 2, "rc": 2, "pre": 2, "preview": 2,
}

#: ``v1.2.3`` | ``1.2`` | ``1.2.3rc1`` | ``v2.12.1-beta`` | ``1.2.3.post1`` | ``1.2.3.dev2``
_VERSION_RE = re.compile(
    r"""^v?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pren>\d+)?)?
    (?:[-_.]?(?P<post>post|rev|r)[-_.]?(?P<postn>\d+)?)?
    (?:[-_.]?dev[-_.]?(?P<devn>\d+)?)?
    $""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """One newer release, ready for the settings card to render."""

    version: str
    notes: str = ""
    published_at: str = ""
    release_url: str = RELEASE_PAGE_URL
    source: str = ""

    @property
    def page_url(self) -> str:
        return self.release_url or RELEASE_PAGE_URL


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """One probe: did anything answer, and is a newer release known.

    ``checked=False`` means "all sources failed" -- offline, DNS, TLS, a yanked
    feed. Callers must treat that differently from a *successful* "nothing newer"
    answer: retry later, and never drop an update notice the user already saw.
    """

    checked: bool
    info: Optional["UpdateInfo"] = None


def _parse(text: object) -> Optional[tuple]:
    """Version text -> comparable tuple, or ``None`` when unparseable.

    Tuple shape: ``(release, is_final, pre_rank, pre_num, post_num, dev_rank, dev_num)``.
    ``release`` is zero-padded at comparison time, so ``1.2`` and ``1.2.0`` tie.

    The prerelease ranks mirror PEP 440 ordering: ``1.0.dev1 < 1.0a1 < 1.0rc1 <
    1.0 < 1.0.post1`` and ``1.0rc1.dev1 < 1.0rc1``.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _VERSION_RE.match(raw)
    if match is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))
    pre = match.group("pre")
    dev_group = match.group("devn")
    has_dev = dev_group is not None
    pre_key = (pre or "").lower()
    # dev-only release sits *below* every alpha; a dev segment after a normal
    # pre-release keeps that pre-release's rank and loses on ``dev_rank``.
    pre_rank = -1 if (has_dev and not pre) else (_PRE_ORDER.get(pre_key, 3) if pre else 3)
    pre_num = int(match.group("pren") or 0)
    has_post = bool(match.group("post"))
    post_num = int(match.group("postn") or 0) if has_post else 0
    # Only a bare ``.devN`` sits below alpha; a dev segment attached to a post
    # release inherits the post release's final rank.
    if pre_rank == -1 and has_post:
        pre_rank = 3
    dev_rank = 0 if has_dev else 1
    dev_num = int(dev_group or 0) if has_dev else 0
    # A post-release counts as final even when it carries its own dev segment:
    # ``1.0.post1.dev1`` sits above ``1.0`` and below ``1.0.post1``.
    is_final = not (pre or (has_dev and not has_post))
    return (release, is_final, pre_rank, pre_num, post_num, dev_rank, dev_num)


def parse_version(text: object) -> Optional[tuple]:
    """Public alias of the parser (kept for tests and callers)."""
    return _parse(text)


def _pad(release: tuple, length: int) -> tuple:
    return release + (0,) * (length - len(release))


def compare_versions(left: object, right: object) -> Optional[int]:
    """``-1`` / ``0`` / ``1`` for ``left`` vs ``right``, ``None`` if either is junk.

    Pre-release ordering follows PEP 440: ``1.0.0rc1 < 1.0.0`` and a release with
    more numeric segments wins (``1.0.1 > 1.0``).
    """
    a = _parse(left)
    b = _parse(right)
    if a is None or b is None:
        return None
    width = max(len(a[0]), len(b[0]))
    release_a, release_b = _pad(a[0], width), _pad(b[0], width)
    if release_a != release_b:
        return -1 if release_a < release_b else 1
    # Same release numbers: a final release outranks any of its prereleases.
    if a[1] != b[1]:
        return 1 if a[1] else -1
    for index in (2, 3, 4, 5, 6):
        if a[index] != b[index]:
            return -1 if a[index] < b[index] else 1
    return 0


def is_prerelease(text: object) -> bool:
    parsed = _parse(text)
    return bool(parsed and not parsed[1])


def is_update_available(current: object, latest: object, *, allow_prerelease: bool = False) -> bool:
    """True when ``latest`` is strictly newer than ``current`` on this channel.

    Prereleases are ignored unless the running build is itself a prerelease
    (beta users keep getting betas) or the caller explicitly opts in.
    """
    if not allow_prerelease and is_prerelease(latest) and not is_prerelease(current):
        return False
    return compare_versions(latest, current) == 1


def parse_feed(payload: object) -> Optional[UpdateInfo]:
    """``update.json`` payload -> :class:`UpdateInfo` (defensive, never raises)."""
    if not isinstance(payload, dict):
        return None
    data: dict[str, Any] = payload
    version = str(data.get("version") or data.get("tag") or "").strip().lstrip("vV")
    if _parse(version) is None:
        return None
    notes = data.get("notes") or data.get("body") or data.get("changelog") or ""
    return UpdateInfo(
        version=version,
        notes=str(notes or ""),
        published_at=str(data.get("published_at") or data.get("date") or ""),
        release_url=str(data.get("release_url") or data.get("url") or "") or RELEASE_PAGE_URL,
        source="feed",
    )


def _release_from_api(entry: object) -> Optional[UpdateInfo]:
    if not isinstance(entry, dict):
        return None
    version = str(entry.get("tag_name") or entry.get("name") or "").strip().lstrip("vV")
    if _parse(version) is None:
        return None
    return UpdateInfo(
        version=version,
        notes=str(entry.get("body") or ""),
        published_at=str(entry.get("published_at") or ""),
        release_url=str(entry.get("html_url") or "") or RELEASE_PAGE_URL,
        source="releases-api",
    )


def parse_releases(
    payload: object,
    *,
    current: object = _CURRENT_VERSION,
    allow_prerelease: Optional[bool] = None,
) -> Optional[UpdateInfo]:
    """Pick the newest usable release from a GitHub releases *list* payload.

    Drafts are always dropped; prereleases are dropped unless the running build
    is a prerelease (or the caller passes ``allow_prerelease=True`` -- the same
    channel rule ``is_update_available`` applies, so the API path cannot disagree
    with the feed path). Returns the newest entry even when it is not newer than
    the caller (channel policy is the caller's job), or ``None`` when nothing
    parses.
    """
    if not isinstance(payload, list):
        return None
    allow_pre = is_prerelease(current) if allow_prerelease is None else bool(allow_prerelease)
    candidates = []
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        if entry.get("prerelease") and not allow_pre:
            continue
        info = _release_from_api(entry)
        if info is not None:
            candidates.append(info)
    if not candidates:
        return None
    return max(candidates, key=lambda info: _parse(info.version))


def _client(*, proxies: Any, doh_url: str, timeout: float) -> httpx.Client:
    """Sync client sharing the app's proxy / DoH / certificate policy."""
    from core import get_httpx_verify
    from core.network.doh import build_http_transport

    transport, trust_env = build_http_transport(
        "proxy",
        list(proxies or []),
        doh_url=doh_url,
        is_async=False,
        retries=2,
        verify=get_httpx_verify(),
        http2=True,
    )
    return httpx.Client(
        transport=transport,
        trust_env=trust_env,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept": "application/vnd.github+json, application/json"},
    )


def _fetch_feed(client: httpx.Client) -> UpdateInfo:
    """Feed payload, or ``ValueError`` when the payload is unusable.

    A 2xx with a body the client cannot read is *not* an answer: raising keeps it
    on the same path as a 404, so the caller neither clears a cached banner nor
    stamps a 24h "nothing newer" TTL on a broken feed.
    """
    response = client.get(_feed_url())
    response.raise_for_status()
    info = parse_feed(response.json())
    if info is None:
        raise ValueError(f"unusable feed payload from {_feed_url()}")
    return info


def _fetch_releases(
    client: httpx.Client, current: object, allow_prerelease: bool,
) -> Optional[UpdateInfo]:
    response = client.get(_releases_url(), params={"per_page": 30})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"unusable releases payload from {_releases_url()}")
    # ``None`` (not ``False``) when the caller did not opt in: that lets
    # ``parse_releases`` keep its own rule -- a beta build keeps seeing betas --
    # instead of being pinned to stable by this wrapper.
    return parse_releases(
        payload, current=current,
        allow_prerelease=True if allow_prerelease else None,
    )


def _probe(
    client: httpx.Client, current: object, allow_prerelease: bool,
) -> "ProbeOutcome":
    """Try each source in order on one client; first usable answer wins.

    ``checked`` separates the two shapes of "no update": a source *answered* and
    nothing is newer (record it, drop any cached banner) versus nothing could be
    reached (retry next launch, keep the banner the user already saw). Without
    that bit, an offline launch would silently erase a real update notice.

    A source whose candidate loses on channel grounds (a beta feed for a stable
    build) does **not** end the probe: the next source may hold the stable
    release that is actually newer, and stopping there is how a beta publish
    hides every subsequent stable from users who did not take the beta.
    """
    checked = False
    sources = (
        ("feed", lambda: _fetch_feed(client)),
        ("releases", lambda: _fetch_releases(client, current, allow_prerelease)),
    )
    for name, fetcher in sources:
        try:
            candidate = fetcher()
        # Transport and payload failures are the expected "no answer" shapes.
        # A programming error (AttributeError/KeyError/TypeError from a schema
        # change) must NOT look like an offline launch -- let it reach the
        # worker's error path, where it is logged loudly.
        except (httpx.HTTPError, ValueError, OSError) as exc:
            logger.debug(f"[Update] {name} source failed: {exc}")
            continue
        checked = True
        if candidate is None:
            # An authoritative "this channel has no release yet"; nothing better
            # can come from a second source.
            break
        if not is_update_available(current, candidate.version, allow_prerelease=allow_prerelease):
            logger.debug(
                f"[Update] {name} candidate rejected: current={current} "
                f"latest={candidate.version}")
            continue
        logger.info(f"[Update] newer release available: {current} -> {candidate.version}")
        return ProbeOutcome(checked=True, info=candidate)
    logger.debug(f"[Update] nothing newer reported (checked={checked})")
    return ProbeOutcome(checked=checked)


def probe_updates(
    *,
    proxies: Any = None,
    doh_url: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    current: object = _CURRENT_VERSION,
    allow_prerelease: bool = False,
    client: Optional[httpx.Client] = None,
) -> "ProbeOutcome":
    """Full outcome of one probe (``checked`` + optional newer release).

    ``client`` is a test seam: pass an ``httpx.Client`` over ``MockTransport``
    and the proxy/DoH policy (plus its thread-blocking setup) is skipped
    entirely. Production callers omit it and get the shared app policy.
    """
    if not update_check_enabled():
        logger.debug(f"[Update] probe disabled by {DISABLE_ENV}")
        return ProbeOutcome(checked=False)
    if client is not None:
        return _probe(client, current, allow_prerelease)
    with _client(proxies=proxies, doh_url=doh_url, timeout=timeout) as owned:
        return _probe(owned, current, allow_prerelease)


def check_for_update(
    *,
    proxies: Any = None,
    doh_url: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    current: object = _CURRENT_VERSION,
    allow_prerelease: bool = False,
    client: Optional[httpx.Client] = None,
) -> Optional[UpdateInfo]:
    """Return the newest release when it is newer than ``current``, else ``None``.

    Thin wrapper over :func:`probe_updates` for callers that only need the
    release. Network failures are folded into ``None``; a programming error in
    the parsing layer still propagates (the worker's error path reports it).
    Callers that must tell "up to date" from "could not check" use
    :func:`probe_updates` instead.
    """
    return probe_updates(
        proxies=proxies, doh_url=doh_url, timeout=timeout, current=current,
        allow_prerelease=allow_prerelease, client=client,
    ).info
