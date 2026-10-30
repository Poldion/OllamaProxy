# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.1] – 2026-10-30

### Changed

- Donation link in the README

## [0.2.0] – 2026-10-28

### Added

- English user interface: language switch (DE/EN) in the web UI, defaults to the browser language and is remembered
  in the browser
- Error messages of the admin endpoints in the language of the request (`X-OllamaProxy-Lang` from the UI, otherwise
  `Accept-Language`)
- Modules are asked in the UI language (`Accept-Language`), so their link labels and details match
- Translation check `scripts/check_i18n.py` (part of the tests): reports missing translations and wrong placeholders
- English versions of the guides (`docs/linux-amd.en.md`, `docs/macos.en.md`), linked from the English README

## [0.1.0] – 2026-10-28

### Added

- Initial public release.
- Released models with fixed `num_ctx`, `keep_alive` and think default; aliases and profiles on the same runner
- OpenAI-compatible endpoints translated to Ollama's native API (streaming, tool calls, images, reasoning)
- Filtered model discovery, neutral penalty values removed so Modelfile values apply
- Image/audio helper for models without vision or audio
- Watchdog that reloads permanent models when they fit into memory
- Memory view for macOS and Linux (AMD GTT/VRAM) with load/unload
- Modules: other services (e.g. ImageGen, FaceRec) with links, memory and freeing after idle time
- Web UI with presets; Docker, systemd and launchd deployment with update/rollback script
- Test suite with a simulated Ollama
