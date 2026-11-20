"""Script-entry service check, migrated out of the upstream preprocess module.

Upstream this ran as a *dispatched action*: ``run_script_preprocess`` was called
from the main window's preprocess step and its result came back through
``_action("open_scriptWin", script_entry_state=..., delay_ms=...)``. In this
repo there is no dispatcher and no main window, so the same work is driven
directly from ``app.start()`` (see ``design.md`` §5).

Kept as-is: the soft-fail contract. aria2 bootstrap / engine start failure
and a missing redis MUST NOT abort startup — they only hide the affected nav
entries and leave the rest (cbg / jsoneri / settings) usable.
"""
from __future__ import annotations

from dataclasses import dataclass
import typing as t

from PySide6.QtCore import qtTrId

import httpx

from core import conf, get_httpx_verify
from core.core import ManagedAsset
from core.network.doh import build_http_transport
from core.variables import ACGG_DOC
from core.website.contracts import PreprocessResult

if t.TYPE_CHECKING:
    from core.script.entry_descriptor import ScriptEntryDescriptor


# Soft-fail UX only: let the user read the missing-service banner before open.
# Must stay short — this is NOT a substitute for fixing bootstrap hangs.
SCRIPT_SERVICE_WARNING_DELAY_MS = 2500


def _script_entry_descriptor(slug: str | None = None) -> "ScriptEntryDescriptor":
    from core.script.entry_descriptor import DEFAULT_SCRIPT_ENTRY_SLUG, get_script_entry

    return get_script_entry(slug or DEFAULT_SCRIPT_ENTRY_SLUG)


@dataclass(frozen=True, slots=True)
class ReleaseAssetResult:
    ready: bool
    cache_hit: bool
    cache_expired: bool
    db_path: t.Any
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScriptServiceStatus:
    aria2_ready: bool
    redis_server_running: bool

    @property
    def all_required_services_running(self) -> bool:
        # Soft gates: acggr-aria2 may fail bootstrap (network / missing binary)
        # without aborting the whole Script preprocess task. Redis is no longer a
        # required service for any entry (pawchive dropped its redis gate); it
        # survives only as a diagnostic payload field for the legacy CLI queue.
        return self.aria2_ready

    @property
    def missing_services(self) -> tuple[str, ...]:
        missing_services: list[str] = []
        if not self.aria2_ready:
            missing_services.append("acggr-aria2")
        return tuple(missing_services)

    def to_payload(self) -> dict[str, bool]:
        return {
            "aria2_ready": self.aria2_ready,
            "redis_server_running": self.redis_server_running,
            "all_required_services_running": self.all_required_services_running,
        }


@dataclass(frozen=True, slots=True)
class ScriptEntryState:
    service_status: ScriptServiceStatus
    entry_slug: str | None = None
    data_ready: bool | None = None
    data_cache_hit: bool | None = None
    data_errors: tuple[str, ...] = ()

    @property
    def _descriptor(self) -> "ScriptEntryDescriptor":
        return _script_entry_descriptor(self.entry_slug)

    @property
    def danbooru_visible(self) -> bool:
        return self.service_status.aria2_ready

    @property
    def service_ready(self) -> bool:
        ready = self.service_status.aria2_ready
        if self._descriptor.requires_redis:
            ready = ready and self.service_status.redis_server_running
        return ready

    @property
    def should_check_data(self) -> bool:
        return self.service_ready

    @property
    def visible(self) -> bool:
        return self.service_ready and self.data_ready is True

    @property
    def cbg_visible(self) -> bool:
        return True

    @property
    def jsoneri_palaces_probe_visible(self) -> bool:
        return True

    @property
    def settings_visible(self) -> bool:
        return True

    @property
    def hidden_entries(self) -> tuple[str, ...]:
        hidden_entries: list[str] = []
        if not self.danbooru_visible:
            hidden_entries.append("Danbooru")
        if not self.visible:
            hidden_entries.append(self._descriptor.display_name)
        return tuple(hidden_entries)

    def with_asset_result(self, asset: ReleaseAssetResult) -> "ScriptEntryState":
        return ScriptEntryState(
            service_status=self.service_status, entry_slug=self.entry_slug, data_ready=asset.ready,
            data_cache_hit=asset.cache_hit, data_errors=asset.errors,
        )

    def to_payload(self) -> dict[str, t.Any]:
        descriptor = self._descriptor
        return {
            "services": self.service_status.to_payload(),
            "aria2_ready": self.service_status.aria2_ready,
            "redis_server_running": self.service_status.redis_server_running,
            "missing_services": self.service_status.missing_services,
            f"{descriptor.slug}_data_ready": self.data_ready,
            f"{descriptor.slug}_data_cache_hit": self.data_cache_hit,
            f"{descriptor.slug}_data_errors": self.data_errors,
            "danbooru_visible": self.danbooru_visible,
            descriptor.visibility_payload_key: self.visible,
            "cbg_visible": self.cbg_visible,
            "jsoneri_palaces_probe_visible": self.jsoneri_palaces_probe_visible,
            "settings_visible": self.settings_visible,
            "hidden_entries": self.hidden_entries,
            f"should_check_{descriptor.slug}_data": self.should_check_data,
        }


class ReleaseAssetCache:
    def __init__(
        self, *, name: str, db_path, download_urls: tuple[str, ...], data_client: httpx.AsyncClient,
        progress_callback=None, label: str | None = None, timeout: int = 30, cache_ttl_hours: int = 480,
    ):
        self.name = name
        self.db_path = db_path
        self.download_urls = download_urls
        self.data_client = data_client
        self.progress_callback = progress_callback
        self.label = label or f"{name} db"
        self.timeout = timeout
        self.cache_ttl_hours = cache_ttl_hours
        self.cache = _EntryCache(self.db_path)

    async def ensure(self) -> ReleaseAssetResult:
        cached_db_path = self.cache.run(lambda: None, self.cache_ttl_hours)
        if cached_db_path:
            return ReleaseAssetResult(True, True, False, t.cast(t.Any, cached_db_path))
        self._emit_legacy_download_start()
        ready, errors = await self._download()
        return ReleaseAssetResult(ready, False, self.cache.state == "expired", self.db_path, tuple(errors))

    def _emit_legacy_download_start(self) -> None:
        progress_start = getattr(self.progress_callback, "download_start", None)
        if callable(self.progress_callback) and not callable(progress_start):
            self.progress_callback(f"{self.label} dling...")

    async def _download(self) -> tuple[bool, list[str]]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.db_path.with_suffix(".db.tmp")
        errors: list[str] = []
        progress_reset = getattr(self.progress_callback, "download_reset", None)
        progress_start = getattr(self.progress_callback, "download_start", None)
        progress_advance = getattr(self.progress_callback, "download_advance", None)
        progress_finish = getattr(self.progress_callback, "download_finish", None)
        try:
            for url in self.download_urls:
                try:
                    if callable(progress_reset):
                        progress_reset(label=self.label)
                    async with self.data_client.stream("GET", url, follow_redirects=True, timeout=self.timeout) as resp:
                        resp.raise_for_status()
                        total_header = resp.headers.get("Content-Length", "").strip()
                        # Use 0 for unknown size so every progress reporter receives
                        # the same integer-only byte-count contract.
                        total_bytes = int(total_header) if total_header.isdigit() else 0
                        if callable(progress_start):
                            progress_start(label=self.label, total_bytes=total_bytes)
                        with open(tmp_path, "wb") as file_obj:
                            async for chunk in resp.aiter_bytes(chunk_size=8192):
                                file_obj.write(chunk)
                                if callable(progress_advance):
                                    progress_advance(len(chunk), label=self.label, total_bytes=total_bytes)
                    import os

                    os.replace(str(tmp_path), str(self.db_path))
                    if callable(progress_finish):
                        progress_finish(label=self.label)
                    return True, errors
                except Exception as exc:
                    tmp_path.unlink(missing_ok=True)
                    errors.append(f"{url}: {exc}")
            return False, errors
        finally:
            tmp_path.unlink(missing_ok=True)


class _EntryCache:
    """Minimal stand-in for ``core.website.core.Cache``.

    The core Cache decorator is written for the mainline site-preprocess call
    sites; the script entry only needs "is the release db at its resolved path
    still fresh". The path is the db itself (``conf_dir/pawchive.db``), never
    ``__temp``.
    """

    def __init__(self, cache_path):
        self.cache_path = cache_path
        self.state = None

    def run(self, func, expiry_hours: int = 168):
        from datetime import datetime, timedelta
        from pathlib import Path

        cache_path = Path(self.cache_path)
        if cache_path.exists():
            file_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
            if datetime.now() - file_time <= timedelta(hours=expiry_hours):
                self.state = "validate"
                return cache_path
            self.state = "expired"
        else:
            self.state = "missing"
        return None


class ScriptEntryReleaseAsset(ReleaseAssetCache):
    def __init__(self, data_client: httpx.AsyncClient, progress_callback=None):
        from core.website.pawchive import authors_db_path

        descriptor = _script_entry_descriptor()
        super().__init__(
            name=descriptor.slug, db_path=authors_db_path(),
            download_urls=ManagedAsset(descriptor.asset_logical_name).urls,
            data_client=data_client, progress_callback=progress_callback, timeout=60,
        )


def _format_script_service_warning(script_entry_state: ScriptEntryState) -> str:
    missing_services = "、".join(script_entry_state.service_status.missing_services)
    hidden_entries = "、".join(script_entry_state.hidden_entries)
    # One translatable format string: f-string fragments would split the Chinese
    # into pieces lupdate can never reassemble. All three arguments stay outside.
    return qtTrId("script.redis_missing.detail") % (
        SCRIPT_SERVICE_WARNING_DELAY_MS // 1000,
        missing_services,
        hidden_entries,
    )


def _format_script_entry_data_warning() -> str:
    return qtTrId("script.cache_check.failed") % _script_entry_descriptor().display_name


def _ensure_data_client(conf_state=conf) -> httpx.AsyncClient:
    proxies = list(getattr(conf_state, "proxies", None) or ())
    doh_url = str(getattr(conf_state, "doh_url", "") or "")
    # Windows 下 httpx 自带 CA 常缺系统根证书，导致 preset db 下载
    # CERTIFICATE_VERIFY_FAILED；get_httpx_verify() 即用系统 CA 的上下文。
    transport, trust_env = build_http_transport(
        "proxy", proxies, doh_url=doh_url, is_async=True, retries=2, verify=get_httpx_verify(),
    )
    return httpx.AsyncClient(transport=transport, trust_env=trust_env)


async def run_script_preprocess(
    *, conf_state=conf, data_client: httpx.AsyncClient | None = None, progress_callback=None
) -> PreprocessResult:
    """Startup service check.

    The ``delay_ms`` open-delay semantics are gone: there is no dispatcher that
    would hold the window back, so the banner is shown by the window itself.
    """
    owns_data_client = data_client is None
    resolved_data_client = data_client

    def ensure_data_client() -> httpx.AsyncClient:
        nonlocal resolved_data_client
        if resolved_data_client is None:
            resolved_data_client = _ensure_data_client(conf_state)
        return resolved_data_client

    try:
        return await _preprocess_script(
            ensure_data_client=ensure_data_client,
            progress_callback=progress_callback,
        )
    finally:
        if owns_data_client and resolved_data_client is not None:
            await resolved_data_client.aclose()


async def _preprocess_script(
    *,
    ensure_data_client: t.Callable[[], httpx.AsyncClient],
    progress_callback=None,
) -> PreprocessResult:
    service_status = _check_script_services(progress_callback=progress_callback)
    script_entry_state = ScriptEntryState(service_status=service_status)

    messages: list[dict[str, t.Any]] = []
    state_flags: dict[str, t.Any] = {
        "services_ready": service_status.all_required_services_running,
        "service_status": service_status.to_payload(),
        "missing_services": service_status.missing_services,
    }

    if service_status.all_required_services_running:
        messages.append(_message("success", qtTrId("script.service_check.ok")))
    else:
        service_fail_message = _message(
            "warning", _format_script_service_warning(script_entry_state), channel="custom",
            title=qtTrId("script.service_check.failed"),
            url=f"{ACGG_DOC}/script",
            url_name=qtTrId("script.guide.title"),
            duration=SCRIPT_SERVICE_WARNING_DELAY_MS,
        )
        messages.append(service_fail_message)

    descriptor = script_entry_state._descriptor
    if script_entry_state.should_check_data:
        data_client = ensure_data_client()
        entry_asset = await ScriptEntryReleaseAsset(data_client, progress_callback).ensure()
        script_entry_state = script_entry_state.with_asset_result(entry_asset)
        state_flags["data_ready"] = entry_asset.ready
        state_flags["data_cache_hit"] = entry_asset.cache_hit
        if entry_asset.errors:
            state_flags[descriptor.db_errors_flag] = entry_asset.errors
        if entry_asset.ready:
            messages.append(_message("success", qtTrId("script.cache_check.ok")))
        else:
            messages.append(_message("warning", _format_script_entry_data_warning()))
    else:
        state_flags[descriptor.check_skipped_flag] = True

    script_entry_state_payload = script_entry_state.to_payload()
    state_flags["script_entry_state"] = script_entry_state_payload
    state_flags["hidden_script_entries"] = script_entry_state.hidden_entries

    return PreprocessResult(
        ready=True, block_search=False, runtime_ready=False, messages=tuple(messages),
        actions=(), state_flags=state_flags,
    )


def _message(level: str, text: str, *, channel: str = "text", **kwargs) -> dict[str, t.Any]:
    return {"level": level, "text": text, "channel": channel, **kwargs}


def _check_script_services(*, progress_callback=None) -> ScriptServiceStatus:
    # acggr-aria2 is acggr-managed. Binary payload download may report AsyncTask
    # progress. Manifest / tiny metadata stays silent whole-body.
    # Bootstrap or engine start failure MUST soft-fail here so Script still
    # opens (CBG / Jsoneri / settings); Danbooru/entry hide via aria2_ready.
    # Redis remains an external soft probe for the redis-backed entry only.
    from loguru import logger

    from core.script import redis_reachable
    from core.script.aria2 import Aria2BinaryBootstrapError, UnsupportedAria2PlatformError, ensure_engine

    if callable(progress_callback):
        progress_callback("checking services...")
    # Redis probe is local TCP only (~150ms). Missing redis MUST NOT delay open
    # beyond SCRIPT_SERVICE_WARNING_DELAY_MS after this returns.
    redis_server_running = redis_reachable()
    try:
        ensure_engine(progress_callback=progress_callback)
        return ScriptServiceStatus(aria2_ready=True, redis_server_running=redis_server_running)
    except (
        Aria2BinaryBootstrapError,
        UnsupportedAria2PlatformError,
        OSError,
        RuntimeError,
    ) as exc:
        logger.warning(
            "[ScriptPreprocess] acggr-aria2 ensure soft-failed ({}): {}",
            type(exc).__name__,
            exc,
        )
        return ScriptServiceStatus(aria2_ready=False, redis_server_running=redis_server_running)
    except Exception as exc:  # pragma: no cover - defensive catch-all keeps Script open
        logger.exception(
            "[ScriptPreprocess] unexpected acggr-aria2 ensure failure; "
            "soft-disabling aria2 to keep Script usable ({}): {}",
            type(exc).__name__,
            exc,
        )
        return ScriptServiceStatus(aria2_ready=False, redis_server_running=redis_server_running)
