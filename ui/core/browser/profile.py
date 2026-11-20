from __future__ import annotations

from pathlib import Path

from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWidgets import QApplication

from core.config import conf_dir
from core.variables import __version__ as _acggr_version


BROWSER_WINDOW_PROFILE_STORAGE_NAME = "cgs-browser-window"
_BROWSER_WINDOW_PROFILE_DIR_NAME = "browser_window_webengine"

#: One QWebEngineProfile per storage directory, for the whole process.
#: QtWebEngine binds one persistent identity (cookie store, localStorage, HTTP cache) to a
#: storage directory; giving each carrier its own profile over the SAME dir loses the shared
#: cookie store (Cloudflare clearance) and leaves later carriers inert. Sharing keeps one store.
_PERSISTENT_PROFILES: dict[str, QWebEngineProfile] = {}


def _qt_object_alive(obj) -> bool:
    """True while the C++ object behind a PySide wrapper still exists."""
    try:
        import shiboken6
    except ImportError:
        try:
            obj.objectName()
        except RuntimeError:
            return False
        return True
    return bool(shiboken6.isValid(obj))


def browser_window_profile_paths(*, base_dir: Path | None = None) -> tuple[Path, Path]:
    root_dir = Path(base_dir or conf_dir).joinpath(_BROWSER_WINDOW_PROFILE_DIR_NAME)
    storage_dir = root_dir.joinpath("storage")
    cache_dir = root_dir.joinpath("cache")
    storage_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir, cache_dir


def create_browser_window_profile(
    parent=None, *, base_dir: Path | None = None, persistent: bool = True,
) -> QWebEngineProfile:
    if not persistent:
        # Off-the-record profile: the danbooru challenge window. It must keep the
        # stock Chromium UA — the site fingerprints it, and the carrier marker
        # below would make the challenge request look like a different client.
        profile = QWebEngineProfile(parent)
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        return profile
    return shared_browser_window_profile(base_dir=base_dir)


def shared_browser_window_profile(*, base_dir: Path | None = None) -> QWebEngineProfile:
    """The one persistent profile for ``base_dir`` (created on first use).

    Never call ``QWebEngineProfile(BROWSER_WINDOW_PROFILE_STORAGE_NAME)`` directly
    for a second carrier: a duplicate profile over a directory that is already in
    use shares the on-disk cache but not the in-memory cookie store, which both
    corrupts the cache and sends the page back through the station's challenge.
    """
    storage_dir, cache_dir = browser_window_profile_paths(base_dir=base_dir)
    key = str(storage_dir)
    cached = _PERSISTENT_PROFILES.get(key)
    if cached is not None and _qt_object_alive(cached):
        return cached
    # Parented to the application, not to a window: the profile is borrowed by
    # carriers that come and go (monitor window, station tabs) and must outlive
    # all of them. A window-owned profile died with its window, and the next
    # carrier then rebuilt a cold identity — the reopen bug.
    profile = QWebEngineProfile(BROWSER_WINDOW_PROFILE_STORAGE_NAME, QApplication.instance())
    profile.setPersistentStoragePath(str(storage_dir))
    profile.setCachePath(str(cache_dir))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    _apply_carrier_user_agent(profile)
    _PERSISTENT_PROFILES[key] = profile
    return profile


_INTERCEPTOR_OWNER_ATTR = "_acggr_profile_interceptor_owner"


def claim_profile_interceptor(profile: QWebEngineProfile, interceptor) -> None:
    """Install ``interceptor`` on a shared profile and remember who owns it.

    A profile outlives every carrier now, so a bare ``setUrlRequestInterceptor``
    from a carrier that later closes leaves the profile holding a pointer to a
    deleted interceptor — the next request then crashes inside QtWebEngine. The
    owner is kept as a weak reference so a closed carrier can hand the slot back
    explicitly (``release_profile_interceptor``).
    """
    import weakref

    profile.setUrlRequestInterceptor(interceptor)
    setattr(profile, _INTERCEPTOR_OWNER_ATTR, weakref.ref(interceptor))


def release_profile_interceptor(profile: QWebEngineProfile, interceptor) -> None:
    """Clear ``interceptor`` from a shared profile, but only if it is still ours.

    A newer carrier may have installed its own interceptor in the meantime; in
    that case this call must not pull the rug out from under it.
    """
    import weakref

    owner_ref = getattr(profile, _INTERCEPTOR_OWNER_ATTR, None)
    owner = owner_ref() if isinstance(owner_ref, weakref.ReferenceType) else None
    if owner is not None and owner is not interceptor:
        return
    try:
        profile.setUrlRequestInterceptor(None)
    except RuntimeError:
        return
    setattr(profile, _INTERCEPTOR_OWNER_ATTR, None)


def _apply_carrier_user_agent(profile: QWebEngineProfile) -> None:
    """Tag every request from this profile with `` Acggr/<version>``.

    The station pages (μsPalace) gate their desk route on the carrier marker in
    the UA: without the suffix the desk entry silently does nothing. Every
    persistent carrier (AriaNg monitor window and the embedded station tabs in
    the WebInterface tab strip) now runs on the one shared profile, so the marker is
    applied once, here, where that profile is built.

    Appended, never replaced: the stock Chromium UA stays intact and sites keep
    seeing a normal browser. Idempotent so a profile re-created over the same
    storage does not end up with the marker twice.
    """
    base = str(profile.httpUserAgent() or "")
    marker = f" Acggr/{_acggr_version}"
    if base.endswith(marker):
        return
    profile.setHttpUserAgent(f"{base}{marker}")
