"""
Python SDK for the Automata platform.

A thin, typed wrapper over the REST API. Errors are mapped to the same
`PlatformError` hierarchy as the server.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from core.errors.errors import PlatformError


@dataclass(slots=True)
class AutomataClient:
    base_url: str = os.environ.get("AUTOMATA_API", "http://localhost:8080")
    token: str = os.environ.get("AUTOMATA_TOKEN", "")
    timeout: float = 30.0

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {}) or {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(base_url=self.base_url, timeout=self.timeout, headers=headers) as c:
            r = c.request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {"message": r.text}
            raise PlatformError(
                body.get("message", "request failed"),
                context={"status": r.status_code, **body},
            )
        return r.json() if r.content else None

    # ---- Automata ----
    def create_automaton(
        self,
        *,
        name: str,
        genesis_prompt: str,
        initial_balance_micro: int = 0,
        currency: str = "USDC",
        parent_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/automata",
            json={
                "name": name,
                "genesis_prompt": genesis_prompt,
                "initial_balance_micro": initial_balance_micro,
                "currency": currency,
                "parent_id": parent_id,
                "metadata": metadata or {},
            },
        )

    def get_automaton(self, aid: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/automata/{aid}")

    def list_automata(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/automata")

    def pause(self, aid: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/automata/{aid}/pause")

    def resume(self, aid: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/automata/{aid}/resume")

    def terminate(self, aid: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/automata/{aid}/terminate")

    def events(self, aid: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/automata/{aid}/events")

    # ---- Treasury ----
    def fund(self, aid: str, *, amount_micro: int, currency: str = "USDC", source: str = "external") -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/treasury/{aid}/fund",
            json={"automaton_id": aid, "amount_micro": amount_micro, "currency": currency, "source": source},
        )

    def balance(self, aid: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/treasury/{aid}/balance")

    def ledger(self, aid: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/treasury/{aid}/ledger", params={"limit": limit})

    # ---- Memory ----
    def memory(self, aid: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/memory/{aid}")

    def write_memory(
        self,
        aid: str,
        *,
        layer: str,
        content: str,
        importance: float = 0.5,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/memory",
            json={
                "automaton_id": aid,
                "layer": layer,
                "content": content,
                "importance": importance,
                "tags": tags or [],
            },
        )

    # ---- Marketplace ----
    def list_offers(self, kind: str | None = None) -> list[dict[str, Any]]:
        params = {"kind": kind} if kind else None
        return self._request("GET", "/v1/marketplace/offers", params=params)

    def create_offer(
        self,
        seller_id: str,
        *,
        kind: str,
        title: str,
        description: str,
        price_micro: int,
        currency: str = "USDC",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/marketplace/offers",
            json={
                "seller_id": seller_id,
                "kind": kind,
                "title": title,
                "description": description,
                "price_micro": price_micro,
                "currency": currency,
            },
        )

    def place_order(self, offer_id: str, buyer_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/marketplace/orders",
            json={"offer_id": offer_id, "buyer_id": buyer_id},
        )

    # ---- Audit ----
    def audit(self, *, automaton_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if automaton_id:
            params["automaton"] = automaton_id
        return self._request("GET", "/v1/audit/log", params=params)

    def verify_audit(self) -> dict[str, Any]:
        return self._request("GET", "/v1/audit/verify")

    # ---- Streaming ----
    def stream_events(self, aid: str) -> Iterator[dict[str, Any]]:
        with httpx.stream(
            "GET",
            f"{self.base_url}/v1/automata/{aid}/events",
            headers={"Authorization": f"Bearer {self.token}"} if self.token else None,
            timeout=None,
        ) as r:
            for line in r.iter_lines():
                if line:
                    yield __import__("json").loads(line)
