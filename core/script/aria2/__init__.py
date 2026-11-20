from core.script.aria2.bootstrap import (
    Aria2BinaryBootstrapError,
    UnsupportedAria2PlatformError,
    ensure_aira2_binary,
)
from core.script.aria2.conf import (
    FROZEN_ARIA2_OPTIONS,
    build_aria2_option_map,
    build_dns_options,
    normalize_proxy,
    render_aria2_conf,
    write_aria2_conf,
)
from core.script.aria2.engine import (
    AcggrAria2Engine,
    RuntimeEndpoint,
    create_managed_rpc_client,
    ensure_engine,
    get_engine,
    stop_engine,
)
from core.script.aria2.rpc import Aria2RpcClient, HTTPX_USER_AGENT, create_aria2_http_client
from core.script.aria2.settings import (
    get_cgs_aria2_section,
    get_proxy,
    set_proxy,
)

__all__ = [
    "Aria2BinaryBootstrapError",
    "Aria2RpcClient",
    "AcggrAria2Engine",
    "FROZEN_ARIA2_OPTIONS",
    "HTTPX_USER_AGENT",
    "RuntimeEndpoint",
    "UnsupportedAria2PlatformError",
    "build_aria2_option_map",
    "build_dns_options",
    "create_aria2_http_client",
    "create_managed_rpc_client",
    "ensure_aira2_binary",
    "ensure_engine",
    "get_cgs_aria2_section",
    "get_engine",
    "get_proxy",
    "normalize_proxy",
    "render_aria2_conf",
    "set_proxy",
    "stop_engine",
    "write_aria2_conf",
]
