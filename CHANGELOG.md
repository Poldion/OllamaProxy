# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

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
