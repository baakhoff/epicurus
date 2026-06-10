# Ollama (local LLM runtime)

[Ollama](https://ollama.com) runs language models locally. The core's **LLM gateway**
(ADR-0010) drives it — chat, embeddings, and **model pull / list** — so models are
fetched and managed by the core at runtime, never baked into an image.

It runs as a **container** so the platform works on any hardware and OS (ADR-0011),
and is **internal-only**: the core reaches it at `http://ollama:11434` on the
`epicurus` network; it is not published to the host.

## CPU by default, GPU opt-in

The default service uses **CPU** inference — it runs everywhere. To use an NVIDIA GPU
(e.g. WSL2 on Windows, or a Linux host) install the NVIDIA Container Toolkit and add
the GPU overlay:

```bash
docker compose -f compose.yaml -f infra/ollama/gpu.yaml up -d
```

Pulled models persist in the `ollama-models` volume. `OLLAMA_KEEP_ALIVE` (default
`5m`) controls idle model unloading (ADR-0005); the core also sets it per request.
