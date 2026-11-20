"""AI package root: kernel-only public surface.

Domain capabilities import from ``core.script.ai.capabilities.<name>``.
"""

from .kernel import (
    AiProvider,
    AiProviderConfigSession,
    AiProviderConfigState,
    AiProviderMgr,
    OpenAiCompatClient,
)

__all__ = [
    "AiProvider",
    "AiProviderConfigSession",
    "AiProviderConfigState",
    "AiProviderMgr",
    "OpenAiCompatClient",
]
