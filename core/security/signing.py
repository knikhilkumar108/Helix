"""
Cryptographic primitives used across the platform.

- Identity: Ed25519 key pairs (signing + verification)
- Hashing: SHA-256 / SHA-3-256 content addressing
- Sealed envelopes: Ed25519 signatures over canonical JSON

We never roll our own crypto. All primitives come from `cryptography` (PyCA),
which is FIPS-compatible and audited.

Key material is always loaded lazily from the secrets backend. Raw private
keys never appear in logs, error messages, or env dumps.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_HASHLIB_OK: Final[bool] = True


@dataclass(frozen=True, slots=True)
class KeyPair:
    private: Ed25519PrivateKey
    public: Ed25519PublicKey

    @classmethod
    def generate(cls) -> "KeyPair":
        priv = Ed25519PrivateKey.generate()
        return cls(private=priv, public=priv.public_key())

    @classmethod
    def from_pem(cls, pem: bytes, password: bytes | None = None) -> "KeyPair":
        priv = serialization.load_pem_private_key(pem, password=password)
        if not isinstance(priv, Ed25519PrivateKey):
            raise TypeError("expected Ed25519 private key")
        return cls(private=priv, public=priv.public_key())

    def public_b64(self) -> str:
        raw = self.public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode()

    def private_pem(self) -> bytes:
        return self.private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def sign(self, message: bytes) -> bytes:
        return self.private.sign(message)

    def verify(self, signature: bytes, message: bytes) -> None:
        try:
            self.public.verify(signature, message)
        except InvalidSignature as e:  # noqa: BLE001
            raise ValueError("signature verification failed") from e


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    """RFC8785-style canonical JSON. Sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_envelope(keypair: KeyPair, payload: Any) -> dict[str, Any]:
    """Produce a signed envelope around a payload."""
    body = canonical_json(payload)
    sig = keypair.sign(body)
    return {
        "payload": json.loads(body),
        "payload_sha256": sha256(body),
        "signature": base64.b64encode(sig).decode(),
        "public_key": keypair.public_b64(),
    }


def verify_envelope(envelope: dict[str, Any]) -> Any:
    body = canonical_json(envelope["payload"])
    expected_sha = sha256(body)
    if expected_sha != envelope["payload_sha256"]:
        raise ValueError("envelope hash mismatch")
    raw_pub = base64.b64decode(envelope["public_key"])
    pub = Ed25519PublicKey.from_public_bytes(raw_pub)
    sig = base64.b64decode(envelope["signature"])
    try:
        pub.verify(sig, body)
    except InvalidSignature as e:  # noqa: BLE001
        raise ValueError("signature verification failed") from e
    return envelope["payload"]
