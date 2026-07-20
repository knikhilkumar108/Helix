"""Auth routes — JWT issuance for the embedded control plane.

In production, identity is federated via OIDC. The local issuer exists so the
embedded runtime and tests can mint and verify tokens without external
dependencies.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.security.signing import KeyPair, canonical_json, sha256

router = APIRouter()


class IssueRequest(BaseModel):
    subject: str
    role: str = "operator"
    ttl_seconds: int = 3600


class IssueResponse(BaseModel):
    token: str
    expires_at: int
    subject: str
    role: str


class VerifyResponse(BaseModel):
    valid: bool
    subject: str | None = None
    role: str | None = None
    expires_at: int | None = None


# For the embedded runtime, use a deterministic but per-process signing key.
_SIGNING = KeyPair.generate()


def _b64url(b: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@router.post("/issue", response_model=IssueResponse)
def issue(req: IssueRequest) -> IssueResponse:
    now = int(time.time())
    exp = now + req.ttl_seconds
    header = {"alg": "EdDSA", "typ": "JWT", "kid": "control-plane-1"}
    payload = {
        "iss": "automata-control-plane",
        "aud": "automata-platform",
        "sub": req.subject,
        "role": req.role,
        "iat": now,
        "nbf": now,
        "exp": exp,
        "jti": uuid.uuid4().hex,
    }
    h_b64 = _b64url(canonical_json(header))
    p_b64 = _b64url(canonical_json(payload))
    signing_input = f"{h_b64}.{p_b64}".encode()
    sig = _SIGNING.sign(signing_input)
    token = f"{h_b64}.{p_b64}.{_b64url(sig)}"
    return IssueResponse(token=token, expires_at=exp, subject=req.subject, role=req.role)


@router.post("/verify", response_model=VerifyResponse)
def verify(authorization: str = Header(...)) -> VerifyResponse:
    try:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise ValueError("scheme")
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("malformed")
        h_b64, p_b64, s_b64 = parts
        signing_input = f"{h_b64}.{p_b64}".encode()
        sig = _b64url_decode(s_b64)
        _SIGNING.verify(sig, signing_input)
        payload = __import__("json").loads(_b64url_decode(p_b64))
        if payload.get("exp", 0) < int(time.time()):
            return VerifyResponse(valid=False)
        return VerifyResponse(
            valid=True, subject=payload.get("sub"), role=payload.get("role"), expires_at=payload.get("exp")
        )
    except Exception:  # noqa: BLE001
        return VerifyResponse(valid=False)
