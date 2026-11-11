"""Built-in local AI engine: a bundled llama.cpp `llama-server` + a bundled GGUF model.

QuantEcho starts the server on demand (first AI request), on 127.0.0.1 with a random free port, and stops
it when the app closes. No Ollama needed. Layout (packaged app, next to QuantEcho.exe):

    engine/llama-server.exe (+ DLLs)        llama.cpp, MIT license
    models/<model>.gguf                      e.g. Qwen3-4B-Q4_K_M.gguf, Apache-2.0

When running from source the same folders are looked up under ./build/ (see packaging/fetch_ai.py).
"""
from __future__ import annotations

import atexit
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .logsetup import log

DEFAULT_MODEL_FILE = "Qwen3-4B-Q4_K_M.gguf"
CTX = 8192


def _roots() -> List[Path]:
    """Places to look for engine/ and models/ (packaged app first, then source checkout)."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    here = Path(__file__).resolve().parent
    roots += [here.parent, here.parent / "build"]
    return roots


def find_engine() -> Optional[Path]:
    for r in _roots():
        p = r / "engine" / "llama-server.exe"
        if p.exists():
            return p
    return None


def find_model(name: str = DEFAULT_MODEL_FILE) -> Optional[Path]:
    for r in _roots():
        p = r / "models" / name
        if p.exists():
            return p
    return None


def available() -> bool:
    return find_engine() is not None and find_model() is not None


# ---------------------------------------------------------------- GPU choice

_INTEGRATED = re.compile(r"\b(UHD|Iris|Vega|Radeon\(TM\) Graphics|Radeon Graphics|Intel\(R\) Graphics|"
                         r"Microsoft Basic|llvmpipe|SwiftShader)\b", re.I)


@dataclass
class Device:
    name: str      # e.g. "Vulkan0"
    label: str     # e.g. "NVIDIA GeForce RTX 3060"
    mib: int


def parse_devices(text: str) -> List[Device]:
    out = []
    for m in re.finditer(r"^\s*(\w+\d+):\s*(.+?)\s*\((\d+)\s*MiB", text, re.M):
        out.append(Device(m.group(1), m.group(2).strip(), int(m.group(3))))
    return out


def pick_device(devs: List[Device]) -> Optional[Device]:
    """Prefer a discrete GPU (most VRAM); integrated graphics only if nothing else; None -> CPU."""
    def score(d: Device):
        discrete = not _INTEGRATED.search(d.label)
        vendor = 2 if re.search(r"NVIDIA|GeForce|RTX|Radeon RX|Arc", d.label, re.I) else 1
        return (discrete, vendor if discrete else 0, d.mib if discrete else 0)
    return max(devs, key=score) if devs else None


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def list_devices(exe: Path) -> List[Device]:
    try:
        r = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=30,
                           creationflags=_no_window())
        return parse_devices(r.stdout + r.stderr)
    except (OSError, subprocess.SubprocessError):
        return []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_command(exe: Path, model: Path, port: int, device: Optional[Device]) -> List[str]:
    cmd = [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(port), "-c", str(CTX),
           "-np", "1", "--jinja", "--reasoning-format", "deepseek", "--no-webui", "--alias", model.stem]
    if device is not None:
        cmd += ["--device", device.name, "-ngl", "auto"]  # fit to FREE VRAM (other apps may hold some)
    else:
        cmd += ["-ngl", "0"]
    return cmd


# ---------------------------------------------------------------- server lifecycle

class Engine:
    """Singleton-ish manager. Thread-safe: ensure_running() may be called from AI worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.device: Optional[Device] = None
        self.model: Optional[Path] = None
        self.last_error: str = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure_running(self, timeout: float = 180.0, status=None) -> str:
        """Start the server if needed and wait until it's healthy. Returns base URL; raises RuntimeError."""
        with self._lock:
            if self.running() and self._healthy():
                return self.base_url
            exe, model = find_engine(), find_model()
            if not exe or not model:
                raise RuntimeError("Built-in AI isn't installed in this build (engine or model file missing).")
            self.stop()
            if status:
                status("detecting GPU…")
            devs = list_devices(exe)
            self.device = pick_device(devs)
            self.model, self.port = model, free_port()
            cmd = build_command(exe, model, self.port, self.device)
            log.info(f"Starting built-in AI: {model.name} on "
                     f"{self.device.label if self.device else 'CPU'} port {self.port}")
            logf = open(Path.home() / ".quanteco_gpt" / "logs" / "engine.log", "ab", buffering=0) \
                if (Path.home() / ".quanteco_gpt" / "logs").exists() else subprocess.DEVNULL
            self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=str(exe.parent),
                                         creationflags=_no_window())
            if status:
                status("loading model…")
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                if self.proc.poll() is not None:
                    self.last_error = f"engine exited with code {self.proc.returncode} (see engine.log)"
                    raise RuntimeError(self.last_error)
                if self._healthy():
                    log.info(f"Built-in AI ready in {time.monotonic() - t0:.1f}s")
                    return self.base_url
                time.sleep(0.25)
            self.stop()
            raise RuntimeError("built-in AI took too long to start")

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as r:
                return r.status == 200
        except (OSError, ValueError):
            return False

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def describe(self) -> str:
        m = (self.model or find_model() or Path(DEFAULT_MODEL_FILE)).stem
        where = self.device.label if self.device else ("CPU" if self.running() else "GPU auto")
        return f"{m} · {where}"


ENGINE = Engine()
atexit.register(ENGINE.stop)
os.environ.setdefault("LLAMA_LOG_COLORS", "off")
