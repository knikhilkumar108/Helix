"""LLM router: provider selection + real provider clients."""
from .real_clients import (
    AnthropicClient,
    OllamaClient,
    OpenAIClient,
    OpenRouterClient,
    default_real_router,
)
from .router import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelClient,
    ModelSpec,
    StubModelClient,
    default_router,
)

__all__ = [
    "AnthropicClient",
    "CompletionRequest",
    "CompletionResponse",
    "LLMRouter",
    "ModelClient",
    "ModelSpec",
    "OllamaClient",
    "OpenAIClient",
    "OpenRouterClient",
    "StubModelClient",
    "default_real_router",
    "default_router",
]
