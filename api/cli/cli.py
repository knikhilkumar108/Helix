"""
Command-line interface for the Automata platform.

Subcommands map 1:1 to REST API endpoints. Output is JSON by default and
human-readable with `--pretty`. Errors include a stable code and a hint to
the docs.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx


def _print(obj: Any, *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        print(json.dumps(obj, separators=(",", ":")))


def _client(args: argparse.Namespace) -> httpx.Client:
    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    return httpx.Client(base_url=args.base_url, headers=headers, timeout=args.timeout)


# ---- commands --------------------------------------------------------
def cmd_create_automaton(args: argparse.Namespace) -> int:
    body = {
        "name": args.name,
        "genesis_prompt": args.genesis_prompt,
        "initial_balance_micro": args.initial_balance_micro,
        "currency": args.currency,
    }
    with _client(args) as c:
        r = c.post("/v1/automata", json=body)
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_list_automata(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get("/v1/automata")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_get_automaton(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get(f"/v1/automata/{args.automaton_id}")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.post(f"/v1/automata/{args.automaton_id}/pause")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.post(f"/v1/automata/{args.automaton_id}/resume")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_terminate(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.post(f"/v1/automata/{args.automaton_id}/terminate")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get("/healthz")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_fund(args: argparse.Namespace) -> int:
    body = {
        "automaton_id": args.automaton_id,
        "amount_micro": args.amount_micro,
        "currency": args.currency,
        "source": args.source,
    }
    with _client(args) as c:
        r = c.post(f"/v1/treasury/{args.automaton_id}/fund", json=body)
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get(f"/v1/treasury/{args.automaton_id}/balance")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get(f"/v1/treasury/{args.automaton_id}/ledger", params={"limit": args.limit})
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get(f"/v1/memory/{args.automaton_id}")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get(f"/v1/automata/{args.automaton_id}/events")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get("/v1/audit/log", params={"limit": args.limit, "automaton": args.automaton_id or ""})
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


def cmd_verify_audit(args: argparse.Namespace) -> int:
    with _client(args) as c:
        r = c.get("/v1/audit/verify")
        r.raise_for_status()
        _print(r.json(), pretty=args.pretty)
    return 0


# ---- parser ----------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automata",
        description="Automata platform CLI",
    )
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--token", default="")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--pretty", action="store_true")

    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("status", help="Service health").set_defaults(func=cmd_status)

    a = sp.add_parser("create", help="Create an automaton")
    a.add_argument("--name", required=True)
    a.add_argument("--genesis-prompt", required=True)
    a.add_argument("--initial-balance-micro", type=int, default=0)
    a.add_argument("--currency", default="USDC")
    a.set_defaults(func=cmd_create_automaton)

    a = sp.add_parser("list", help="List automata")
    a.set_defaults(func=cmd_list_automata)

    a = sp.add_parser("get", help="Get an automaton")
    a.add_argument("automaton_id")
    a.set_defaults(func=cmd_get_automaton)

    for name, fn in [
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("terminate", cmd_terminate),
    ]:
        a = sp.add_parser(name, help=f"{name.title()} an automaton")
        a.add_argument("automaton_id")
        a.set_defaults(func=fn)

    a = sp.add_parser("fund", help="Fund an automaton")
    a.add_argument("automaton_id")
    a.add_argument("--amount-micro", type=int, required=True)
    a.add_argument("--currency", default="USDC")
    a.add_argument("--source", default="external")
    a.set_defaults(func=cmd_fund)

    a = sp.add_parser("balance", help="Get an automaton's balance")
    a.add_argument("automaton_id")
    a.set_defaults(func=cmd_balance)

    a = sp.add_parser("ledger", help="Get the treasury ledger")
    a.add_argument("automaton_id")
    a.add_argument("--limit", type=int, default=50)
    a.set_defaults(func=cmd_ledger)

    a = sp.add_parser("memory", help="List memory entries")
    a.add_argument("automaton_id")
    a.set_defaults(func=cmd_memory)

    a = sp.add_parser("logs", help="Tail the event log for an automaton")
    a.add_argument("automaton_id")
    a.set_defaults(func=cmd_logs)

    a = sp.add_parser("audit", help="Read the audit log")
    a.add_argument("--automaton-id", default=None)
    a.add_argument("--limit", type=int, default=100)
    a.set_defaults(func=cmd_audit)

    a = sp.add_parser("verify-audit", help="Verify the audit chain")
    a.set_defaults(func=cmd_verify_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except httpx.HTTPError as e:
        print(f"http error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
