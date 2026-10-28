# Third-party software and models

This repository contains original code under the [MIT License](LICENSE). The following third-party components are
used. None of them is included in the repository; they are fetched from their official sources during installation or
when the images are built.

## Runtime software

| Component | License | Note |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama) | MIT | Official Docker image `ollama/ollama` or installer |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause | |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | |
| [HTTPX](https://github.com/encode/httpx) / httpcore | BSD-3-Clause | |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | tests only |

## Models

The proxy ships no models. Ollama downloads language models (e.g. from ollama.com); each model has its own license
(e.g. Gemma Terms of Use, Apache-2.0, Qwen licenses). Whoever downloads a model must follow its terms. The model card
of the source is authoritative.

## Images

`docs/bilder/ollamaproxy.png` is an original screenshot of the UI (demo data) and is licensed under this repository's
MIT License.
