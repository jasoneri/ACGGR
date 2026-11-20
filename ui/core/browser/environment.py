from __future__ import annotations

from .types import BrowserCookieSet, BrowserEnvironmentConfig


def build_browser_environment(browser) -> BrowserEnvironmentConfig:
    """独立应用（acggr）没有站点运行时：不注入代理 / DoH / 站点 cookie 目录。

    保留函数形状是因为 ``apply_standard_environment`` 仍要对 challenge 窗口调用它；
    上游那套 ``gui_site_runtime`` 环境构建不属于本仓库。
    """
    return BrowserEnvironmentConfig()
