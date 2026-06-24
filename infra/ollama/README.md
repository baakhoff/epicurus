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

## KV-cache quantization (ADR-0046)

`OLLAMA_KV_CACHE_TYPE` (default `f16`) quantizes the attention KV cache so a longer
context fits in less VRAM — `q8_0` ≈ half, `q4_0` ≈ a quarter, for a small quality
trade-off. The quantized types need flash attention, so set `OLLAMA_FLASH_ATTENTION=1`
alongside them:

```bash
OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_FLASH_ATTENTION=1 docker compose up -d ollama
```

These are **server-wide startup flags** read by Ollama when it boots — they are not
per-request, and the core deliberately cannot restart Ollama (it's a protected
container). The Models page lets the operator record a preferred KV-cache type and shows
this exact instruction; **changing it requires setting the env and restarting Ollama**.
