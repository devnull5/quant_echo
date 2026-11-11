"""Download the built-in AI (pinned + hash-verified) into build/ for development and packaging.

    python packaging/fetch_ai.py

- llama.cpp server, Windows Vulkan build (MIT)        -> build/engine/
- Qwen3-4B Q4_K_M GGUF from Qwen's official repo (Apache-2.0) -> build/models/
- their license texts                                  -> build/licenses/

Idempotent: files whose SHA-256 already matches are not downloaded again. To upgrade, change the pins.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"

ENGINE_TAG = "b12110"  # = llama.cpp stable release v0.9.0
ENGINE_ZIP = f"llama-{ENGINE_TAG}-bin-win-vulkan-x64.zip"
ENGINE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{ENGINE_TAG}/{ENGINE_ZIP}"
ENGINE_SHA = "9732a5e48887ca54d0bfe4af4324dc85fd56cd8cda19b52df3542fa6ebe6874f"

MODEL_FILE = "Qwen3-4B-Q4_K_M.gguf"
MODEL_URL = f"https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/{MODEL_FILE}"
MODEL_SHA = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"

LICENSES = {
    "llama.cpp-LICENSE.txt": f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{ENGINE_TAG}/LICENSE",
    "Qwen3-LICENSE.txt": "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/LICENSE",
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 24), b""):
            h.update(b)
    return h.hexdigest()


def fetch(url: str, dest: Path, expect: str = "") -> Path:
    if dest.exists() and (not expect or sha256(dest) == expect):
        print(f"ok (cached)  {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "QuantEcho-build"})
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as f:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB", end="", flush=True)
    print()
    if expect and (got := sha256(tmp)) != expect:
        tmp.unlink()
        sys.exit(f"SHA-256 mismatch for {dest.name}: got {got}, expected {expect}")
    tmp.replace(dest)
    print(f"verified     {dest.name}")
    return dest


def main() -> None:
    z = fetch(ENGINE_URL, BUILD / "downloads" / ENGINE_ZIP, ENGINE_SHA)
    eng = BUILD / "engine"
    if not (eng / "llama-server.exe").exists():
        shutil.rmtree(eng, ignore_errors=True)
        with zipfile.ZipFile(z) as zf:
            bad = [n for n in zf.namelist() if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/")]
            if bad:
                sys.exit(f"refusing unsafe zip paths: {bad[:3]}")
            zf.extractall(eng)
        print(f"extracted    {eng}")
    fetch(MODEL_URL, BUILD / "models" / MODEL_FILE, MODEL_SHA)
    for name, url in LICENSES.items():
        fetch(url, BUILD / "licenses" / name)
    omp = eng / "LICENSE-LLVM-OpenMP"
    if omp.exists():
        shutil.copy2(omp, BUILD / "licenses" / "LLVM-OpenMP-LICENSE.txt")
    print("built-in AI ready in build/")


if __name__ == "__main__":
    main()
