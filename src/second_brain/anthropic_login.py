from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
from typing import Any

from .llm import LLMError

URL_RE = re.compile(r"https://[^\s'\"<>]+")
START_TIMEOUT = 25.0
COMPLETE_TIMEOUT = 180.0
RELEASES = "https://github.com/anthropics/anthropic-cli/releases"


def ant_binary() -> str | None:
    return shutil.which("ant")


def install_instructions() -> list[str]:
    if sys.platform == "darwin":
        return [
            "brew install anthropics/tap/ant",
            'xattr -d com.apple.quarantine "$(brew --prefix)/bin/ant"',
        ]
    if sys.platform == "win32":
        return [
            f"Download ant for Windows from {RELEASES} and put ant.exe on your PATH",
            "or, with Go installed: go install github.com/anthropics/anthropic-cli/cmd/ant@latest",
        ]
    return [
        f"Download the Linux release from {RELEASES} and copy ant to /usr/local/bin",
        "or, with Go installed: go install github.com/anthropics/anthropic-cli/cmd/ant@latest",
    ]


async def profile_status() -> str | None:
    binary = ant_binary()
    if binary is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "auth",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
    except (OSError, TimeoutError):
        return None
    text = output.decode("utf-8", "replace").strip()
    return text[:600] or None


class LoginFlow:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.output: list[str] = []
        self.url: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.returncode: int | None = None
        self._reader: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.returncode is None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "url": self.url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "output": "\n".join(self.output[-20:]),
        }

    async def start(self) -> dict[str, Any]:
        binary = ant_binary()
        if binary is None:
            raise LLMError(
                "the Anthropic CLI (ant) is not installed on this server; "
                + "; ".join(install_instructions()),
                409,
            )
        await self.cancel()
        self.output = []
        self.url = None
        self.finished_at = None
        self.returncode = None
        self.started_at = time.time()
        self.process = await asyncio.create_subprocess_exec(
            binary,
            "auth",
            "login",
            "--no-browser",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader = asyncio.get_running_loop().create_task(self._read())
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.url:
                return self.status()
            if self.returncode is not None:
                break
            await asyncio.sleep(0.2)
        await self.cancel()
        raise LLMError(
            "ant auth login did not print a sign-in link; output: "
            + " | ".join(self.output[-5:])[:300],
            502,
        )

    async def submit_code(self, code: str) -> dict[str, Any]:
        if not self.running or self.process is None or self.process.stdin is None:
            raise LLMError("start the sign-in first", 409)
        cleaned = code.strip()
        if not cleaned:
            raise LLMError("paste the code shown after signing in", 400)
        self.process.stdin.write((cleaned + "\n").encode())
        await self.process.stdin.drain()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=COMPLETE_TIMEOUT)
        except TimeoutError as error:
            await self.cancel()
            raise LLMError("the sign-in did not finish in time; start it again", 502) from error
        self.returncode = self.process.returncode
        self.finished_at = time.time()
        if self.returncode != 0:
            raise LLMError(
                "the sign-in failed: " + (" | ".join(self.output[-5:])[:300] or "no details"),
                401,
            )
        return self.status()

    async def cancel(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                pass
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self.process is not None and self.returncode is None:
            self.returncode = self.process.returncode
        self.process = None

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.output.append(text)
                match = URL_RE.search(text)
                if match and self.url is None:
                    self.url = match.group(0).rstrip(".,)")
        self.returncode = await self.process.wait()
        self.finished_at = time.time()
