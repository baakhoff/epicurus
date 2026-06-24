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

## KV-cache quantization (ADR-0046, #307)

`OLLAMA_KV_CACHE_TYPE` (default `f16`) quantizes the attention KV cache so a longer
context fits in less VRAM — `q8_0` ≈ half, `q4_0` ≈ a quarter, for a small quality
trade-off. The quantized types need flash attention; the Models-page picker enables
`OLLAMA_FLASH_ATTENTION` for you when you choose one.

Because these are **server-wide startup flags** (read when Ollama boots, not per-request),
applying a change means restarting Ollama. Picking a type on the **Models page** does this
for you (#307): the core writes the values to a small env file in a shared volume and restarts
the Ollama container through its tightly-scoped Docker access (restart-only, allowlisted to
`ollama`). The Ollama entrypoint sources that file on every (re)start, so the choice takes
effect on restart and persists across reconciles.

If the core can't reach Docker (no socket mounted) it still saves the choice, and the UI falls
back to the manual path — set the env and bounce Ollama yourself:

```bash
OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_FLASH_ATTENTION=1 docker compose up -d ollama
```

(Use `docker compose up -d`, not `restart`: compose-level env is fixed at container create, so
the container must be recreated to pick up a new value that way.)
