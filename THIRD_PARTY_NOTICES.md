# Third-party notices

QuantEcho itself is MIT-licensed (see [LICENSE](LICENSE)). The Windows/Microsoft Store build also ships:

| Component | Version | License | Source |
|---|---|---|---|
| llama.cpp (`llama-server` + ggml libraries, Vulkan build) | b12110 (v0.9.0) | MIT | https://github.com/ggml-org/llama.cpp |
| LLVM OpenMP runtime (`libomp.dll`, part of the llama.cpp build) | — | Apache-2.0 with LLVM exception | https://openmp.llvm.org |
| Qwen3-4B, Q4_K_M GGUF quantisation by the Qwen team | — | Apache-2.0 | https://huggingface.co/Qwen/Qwen3-4B-GGUF |
| Python runtime + Tcl/Tk (bundled by PyInstaller) | 3.13 / 8.6 | PSF / Tcl license | https://python.org |
| httpx, keyring, python-dotenv, tzdata, certifi | see pyproject | BSD / MIT / Apache-2.0 / MPL-2.0 | PyPI |

Full license texts are installed next to the app in `licenses/`. The Qwen3 model is used unmodified.
Model outputs are generated locally and may be inaccurate; they are not financial advice.
