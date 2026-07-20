"""
Centralized error types. Every error carries:
  - a stable code (used in APIs and metrics)
  - a human-readable message
  - optional structured context (kept small, never PII)
  - a category for log routing

Errors are intentionally not exceptions over the wire. Services raise these
internally and translate to gRPC/REST status codes at the edge.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class ErrorCategory(str, Enum):
    VALIDATION = "validation"
    AUTH = "auth"
    POLICY = "policy"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMIT = "rate_limit"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    SANDBOX = "sandbox"
    EXTERNAL = "external"
    INTERNAL = "internal"


class PlatformError(Exception):
    code: str = "platform.internal"
    category: ErrorCategory = ErrorCategory.INTERNAL
    http_status: int = 500
    grpc_code: str = "INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        context: Optional[dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "context": self.context,
        }


class ValidationError(PlatformError):
    code = "platform.validation"
    category = ErrorCategory.VALIDATION
    http_status = 400
    grpc_code = "INVALID_ARGUMENT"


class AuthError(PlatformError):
    code = "platform.auth"
    category = ErrorCategory.AUTH
    http_status = 401
    grpc_code = "UNAUTHENTICATED"


class ForbiddenError(PlatformError):
    code = "platform.forbidden"
    category = ErrorCategory.POLICY
    http_status = 403
    grpc_code = "PERMISSION_DENIED"


class NotFoundError(PlatformError):
    code = "platform.not_found"
    category = ErrorCategory.NOT_FOUND
    http_status = 404
    grpc_code = "NOT_FOUND"


class ConflictError(PlatformError):
    code = "platform.conflict"
    category = ErrorCategory.CONFLICT
    http_status = 409
    grpc_code = "ALREADY_EXISTS"


class RateLimitError(PlatformError):
    code = "platform.rate_limited"
    category = ErrorCategory.RATE_LIMIT
    http_status = 429
    grpc_code = "RESOURCE_EXHAUSTED"


class ResourceExhaustedError(PlatformError):
    code = "platform.resource_exhausted"
    category = ErrorCategory.RESOURCE_EXHAUSTED
    http_status = 507
    grpc_code = "RESOURCE_EXHAUSTED"


class SandboxError(PlatformError):
    code = "platform.sandbox"
    category = ErrorCategory.SANDBOX
    http_status = 500
    grpc_code = "FAILED_PRECONDITION"


class PolicyDeniedError(PlatformError):
    code = "platform.policy_denied"
    category = ErrorCategory.POLICY
    http_status = 403
    grpc_code = "PERMISSION_DENIED"


class InsufficientFundsError(PlatformError):
    code = "platform.insufficient_funds"
    category = ErrorCategory.RESOURCE_EXHAUSTED
    http_status = 402
    grpc_code = "FAILED_PRECONDITION"


class ConstitutionViolationError(PlatformError):
    code = "platform.constitution_violation"
    category = ErrorCategory.POLICY
    http_status = 403
    grpc_code = "PERMISSION_DENIED"
