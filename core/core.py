import re
import typing as t
from dataclasses import dataclass

from core.variables import ASSETS_FALLBACK, GITHUB_PRESET_BASE, IMGBED_ASSET_OBJECTS

ILLEGAL_FILENAME_RE = re.compile(r'[|:<>?*"\\/]')


__all__ = ["ILLEGAL_FILENAME_RE", "sanitize_for_path", "ManagedAsset"]


def sanitize_for_path(name: str) -> str:
    return ILLEGAL_FILENAME_RE.sub('-', name)


@dataclass(frozen=True)
class ManagedAsset:
    """托管 preset 下载源 (GitHub preset 主源，ImgBed res 兜底)。"""

    logical_name: str

    @property
    def github_url(self) -> str:
        return f"{GITHUB_PRESET_BASE.rstrip('/')}/{self.logical_name.lstrip('/')}"

    @property
    def fallback_url(self) -> t.Optional[str]:
        object_name = IMGBED_ASSET_OBJECTS.get(self.logical_name.strip())
        if not object_name:
            return None
        return f"{ASSETS_FALLBACK.rstrip('/')}/{object_name.lstrip('/')}"

    @property
    def urls(self) -> t.Tuple[str, ...]:
        """Ordered download URLs: GitHub preset first, ImgBed res fallback when mapped."""
        urls = [self.github_url]
        fallback_url = self.fallback_url
        if fallback_url:
            urls.append(fallback_url)
        return tuple(urls)

    @property
    def sources(self) -> t.Tuple[t.Dict[str, str], ...]:
        """Ordered (id, url) sources for bootstrap-style failover loops."""
        sources = [{"id": "github", "url": self.github_url}]
        fallback_url = self.fallback_url
        if fallback_url:
            sources.append({"id": "imgbed", "url": fallback_url})
        return tuple(sources)
