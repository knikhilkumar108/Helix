"""Unit tests for the Ed25519 signing helpers."""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.security.signing import (
    KeyPair,
    canonical_json,
    sha256,
    sign_envelope,
    verify_envelope,
)


def test_sha256():
    assert sha256(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert len(sha256(b"x")) == 64


def test_canonical_json_sorts_keys():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


def test_keypair_sign_and_verify():
    kp = KeyPair.generate()
    sig = kp.sign(b"hello")
    kp.verify(sig, b"hello")
    with pytest.raises(ValueError):
        kp.verify(sig, b"hello!")


def test_envelope_roundtrip():
    kp = KeyPair.generate()
    payload = {"hello": "world", "n": 42}
    env = sign_envelope(kp, payload)
    decoded = verify_envelope(env)
    assert decoded == payload


def test_envelope_tamper_detected():
    kp = KeyPair.generate()
    env = sign_envelope(kp, {"x": 1})
    env["payload"]["x"] = 2
    with pytest.raises(ValueError):
        verify_envelope(env)


def test_public_b64_round_trip():
    kp = KeyPair.generate()
    raw = base64.b64decode(kp.public_b64())
    Ed25519PublicKey.from_public_bytes(raw)  # should not raise
