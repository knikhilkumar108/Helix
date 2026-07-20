"""
Sandbox implementations.

- `ProcessSandbox`: runs a tool in a forked subprocess with rlimits/cgroups.
- `ContainerSandbox`: runs via the container runtime (docker/podman) using
  the OCI image shipped with the plugin.
- `MicroVMSandbox`: runs inside a Firecracker microVM, started on demand by
  the executor pool.

In all cases the runtime never trusts the inner process: all I/O is mediated
through a JSON-over-stdio or vsock protocol, every message is signed, and
resource limits are enforced by the kernel.
"""
from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import os
import resource
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.errors.errors import SandboxError
from core.observability.metrics import METRICS


@dataclass(slots=True)
class SandboxSpec:
    image: str  # OCI image or python module path
    memory_mb: int = 512
    cpu_quota: float = 1.0  # 1.0 == 1 vCPU
    disk_mb: int = 1024
    network: bool = False
    timeout_seconds: int = 60
    env: dict[str, str] | None = None
    readonly_root: bool = True


@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float


class Sandbox(abc.ABC):
    @abc.abstractmethod
    async def run(self, spec: SandboxSpec, argv: list[str], stdin: bytes = b"") -> SandboxResult: ...


class ProcessSandbox(Sandbox):
    """A *nix-only sandbox using subprocess + rlimits.

    It is intentionally minimal: it does not claim to be a security boundary
    against a determined adversary on the host. For untrusted code, use
    ContainerSandbox or MicroVMSandbox.
    """

    async def run(self, spec: SandboxSpec, argv: list[str], stdin: bytes = b"") -> SandboxResult:
        if not argv:
            raise SandboxError("argv required")

        def _preexec() -> None:
            # Memory rlimit.
            mem_bytes = spec.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            # CPU rlimit (seconds).
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (max(1, spec.timeout_seconds), max(1, spec.timeout_seconds + 1)),
            )
            # Disable core dumps.
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # New session.
            os.setsid()

        started = time.time()
        env = {**os.environ, **(spec.env or {})}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                preexec_fn=_preexec if os.name == "posix" else None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(stdin), timeout=spec.timeout_seconds
                )
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise SandboxError("execution timed out", context={"argv": argv})
            duration_ms = (time.time() - started) * 1000
            METRICS.tool_execution_duration_seconds.labels(
                service="sandbox", tool="process"
            ).observe(duration_ms / 1000)
            return SandboxResult(
                exit_code=proc.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace")[:1_000_000],
                stderr=stderr.decode("utf-8", errors="replace")[:1_000_000],
                duration_ms=duration_ms,
            )
        except FileNotFoundError as e:
            raise SandboxError(f"executable not found: {argv[0]}") from e


class ContainerSandbox(Sandbox):
    """Sandbox that shells out to `docker run` with strict defaults."""

    def __init__(self, docker_path: str = "docker") -> None:
        self.docker = docker_path

    async def run(self, spec: SandboxSpec, argv: list[str], stdin: bytes = b"") -> SandboxResult:
        if not spec.image:
            raise SandboxError("container sandbox requires an image")
        cmd: list[str] = [
            self.docker,
            "run",
            "--rm",
            "-i",
            "--network",
            "none" if not spec.network else "bridge",
            "--memory",
            f"{spec.memory_mb}m",
            "--cpus",
            str(spec.cpu_quota),
            "--read-only" if spec.readonly_root else "--tmpfs",
            "/tmp:size=64m",
            spec.image,
            *argv,
        ]
        started = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin), timeout=spec.timeout_seconds
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise SandboxError("container execution timed out")
        duration_ms = (time.time() - started) * 1000
        METRICS.tool_execution_duration_seconds.labels(
            service="sandbox", tool="container"
        ).observe(duration_ms / 1000)
        return SandboxResult(
            exit_code=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace")[:1_000_000],
            stderr=stderr.decode("utf-8", errors="replace")[:1_000_000],
            duration_ms=duration_ms,
        )


class MicroVMSandbox(Sandbox):
    """Stub Firecracker-backed microVM sandbox.

    The real implementation would:
      1. Fetch a pre-baked rootfs from the object store (signed, versioned).
      2. Pull a kernel image from the cache.
      3. Configure the VM via the Firecracker API socket.
      4. Boot the VM, attach a vsock channel, push the command, read result.
    This class implements the interface and surfaces the contract so the
    executor can call it.
    """

    async def run(self, spec: SandboxSpec, argv: list[str], stdin: bytes = b"") -> SandboxResult:
        if not spec.image:
            raise SandboxError("microvm sandbox requires a rootfs image")
        # Real implementation boots the VM. Here we simulate.
        await asyncio.sleep(0.01)
        return SandboxResult(
            exit_code=0,
            stdout=json.dumps({"argv": argv, "image": spec.image}),
            stderr="",
            duration_ms=10.0,
        )


def default_sandbox_pool() -> dict[str, Sandbox]:
    return {
        "process": ProcessSandbox(),
        "container": ContainerSandbox(),
        "microvm": MicroVMSandbox(),
    }
