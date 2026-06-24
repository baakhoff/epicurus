"""Host system + GPU probe behind the context-window suggestion (#context-window).

The Models page asks "how big a context can this box hold?". Answering it needs the
GPU's VRAM (or, with no GPU, system RAM) and the active model's on-disk size. This
module gathers that — **best-effort and graceful**: every probe is wrapped so a missing
tool or unreadable file degrades to ``None`` rather than raising, and nothing shells out
at import time.

GPU detection is multi-vendor but only NVIDIA is exercised on the box this ships to; the
AMD and Intel paths read ``/sys/class/drm`` (or ``rocm-smi``) and are written to be safe —
they return ``None`` on any failure. Detection is factored so the subprocess runner and
file reads are **injectable**, which is what makes each vendor's path unit-testable by
feeding it canned output instead of touching real hardware.

The suggestion (:func:`suggest_context`) is an explicit *estimate* from a rough,
documented KV-cache-per-token heuristic — it is a sane starting point the operator can
override, not a measured maximum.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypeVar

from fastapi import APIRouter
from pydantic import BaseModel

from epicurus_core import get_logger
from epicurus_core_app.llm.models import ModelDetails, ModelInfo

log = get_logger("epicurus_core_app.system_info")

# A subprocess runner: takes argv, returns stdout (or raises). Injectable for tests.
Runner = Callable[[list[str]], str]
# A file reader: takes a path, returns its text (or raises). Injectable for tests.
FileReader = Callable[[str], str]
# A glob: takes a pattern, returns matching path strings. Injectable for tests.
Globber = Callable[[str], list[str]]

# ── Suggestion heuristic constants ────────────────────────────────────────────────
# Headroom carved out of VRAM for the runtime, CUDA context, and activation/compute
# buffers before the KV cache — a deliberately generous flat reserve (an estimate).
_HEADROOM_MB = 1024
# Rough KV-cache cost per token, in MB. The real cost scales with layers * hidden-size *
# 2 (K and V) * bytes-per-element, which is itself ~proportional to model size. We model
# that as a simple linear function of model size (see ``_kv_per_token_mb``) anchored on a
# mid-size (~4.7 GB / 8B-class) model needing ~0.18 MB/token at f16 KV — enough to keep the
# suggestion conservative rather than optimistic. This is an estimate, not a measurement.
_KV_PER_TOKEN_AT_REF_MB = 0.18
_REF_MODEL_SIZE_MB = 4700.0
# Clamp bounds for the computed maximum context.
_MIN_CONTEXT = 2048
# The *fallback* ceiling when the model's trained context length is unknown (hosted model, or
# the runtime didn't report it). It is no longer a universal cap: when the trained length is
# known, that becomes the ceiling instead, so a long-context model on a roomy GPU is no longer
# clipped to 32k (#context-accuracy).
_MAX_CONTEXT = 32768
# Preferred floor for the *suggested* value when it fits (a comfortable working context).
_PREFERRED_SUGGESTED = 8192
# With no GPU we lean on system RAM but cap hard — CPU inference with a big context is slow,
# and RAM is shared with everything else on the box.
_NO_GPU_MAX_CONTEXT = 8192
# KV-cache memory scales with the cache's bytes-per-element. The per-token cost below is
# anchored on f16 (2 bytes); a quantized cache (the operator's OLLAMA_KV_CACHE_TYPE, #310)
# stores fewer bytes per element, so the same memory buys proportionally more context: q8_0
# (~1 byte) ≈ half the cost, q4_0 (~½ byte) ≈ a quarter. Unknown/None → the f16 baseline.
_KV_CACHE_FACTOR: dict[str, float] = {"f16": 1.0, "q8_0": 0.5, "q4_0": 0.25}


def _kv_cache_factor(kv_cache_type: str | None) -> float:
    """The per-token KV-cost multiplier for a cache type (1.0 for f16/unknown)."""
    return _KV_CACHE_FACTOR.get((kv_cache_type or "").lower(), 1.0)


class GpuInfo(BaseModel):
    """A detected GPU. ``vram_free_mb`` is ``None`` when the vendor can't report it."""

    vendor: str  # "nvidia" | "amd" | "intel" | "unknown"
    name: str
    vram_total_mb: int
    vram_free_mb: int | None = None


class CpuInfo(BaseModel):
    """The host CPU. Core counts are ``None`` when they can't be determined."""

    model: str
    physical_cores: int | None = None
    logical_cores: int | None = None


class ModelSize(BaseModel):
    """The currently-effective chat model and the facts behind the suggestion."""

    name: str
    size_mb: int | None = None
    # The model's trained maximum context (from /api/show). The real ceiling for the
    # suggestion — None when unknown (hosted model, or the runtime didn't report it).
    context_length: int | None = None
    # Weight quantization (e.g. "Q4_K_M"), fixed at pull. It already determines the on-disk
    # size that's counted; surfaced so the suggestion's reasoning is legible in the UI.
    quantization: str | None = None


class SuggestedContext(BaseModel):
    """A suggested context-window range (an estimate, not a hard maximum)."""

    min: int
    suggested: int
    max: int


class SystemInfo(BaseModel):
    """What the Models page needs: the host spec + a context-window suggestion."""

    gpu: GpuInfo | None = None
    cpu: CpuInfo | None = None
    ram_total_mb: int | None = None
    model: ModelSize | None = None
    suggested_context: SuggestedContext | None = None
    # The operator's active Ollama KV-cache type (factored into the suggestion); None = the
    # runtime default (f16). Surfaced so the UI can show what the estimate assumed.
    kv_cache_type: str | None = None


def _default_runner(argv: list[str]) -> str:
    """Run ``argv`` and return stdout; raises on non-zero exit or a missing binary."""
    # Fixed argv lists, no shell, no user input — safe to run directly.
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=True)
    return completed.stdout


def _default_file_reader(path: str) -> str:
    return Path(path).read_text()


def _default_globber(pattern: str) -> list[str]:
    # Patterns we use are anchored at /sys; resolve them relative to the filesystem root.
    return [str(p) for p in Path("/").glob(pattern.lstrip("/"))]


def _next_power_of_two_at_most(value: int) -> int:
    """The largest power of two ``<= value`` (``value`` assumed ``>= 1``)."""
    power = 1
    while power * 2 <= value:
        power *= 2
    return power


def _kv_per_token_mb(model_size_mb: float) -> float:
    """A rough KV-cache cost per token (MB), modeled proportional to model size.

    Bigger models have more/larger layers, so their KV cache costs more per token. We scale
    a reference cost (a ~4.7 GB model) linearly by size. This is an intentional
    approximation — the suggestion is an estimate, and erring high keeps it conservative.
    """
    if model_size_mb <= 0:
        return _KV_PER_TOKEN_AT_REF_MB
    return _KV_PER_TOKEN_AT_REF_MB * (model_size_mb / _REF_MODEL_SIZE_MB)


def suggest_context(
    vram_total_mb: int | None,
    model_size_mb: int | None,
    *,
    cap: int = _MAX_CONTEXT,
    kv_cache_type: str | None = None,
    model_max: int | None = None,
) -> SuggestedContext:
    """Suggest a context-window range from available memory and the model's size.

    ``vram_total_mb`` is GPU VRAM when a GPU was detected, else system RAM (the caller
    decides which to pass). The math, all an **estimate**:

    * ``available = vram_total - model_size - headroom`` (memory left for the KV cache).
    * ``per_token = kv_per_token(model) * kv_cache_factor`` — a quantized cache
      (``kv_cache_type`` ``q8_0``/``q4_0``) costs less per token, so the same memory buys
      more context (see :func:`_kv_cache_factor`).
    * ``max = clamp(available / per_token, [2048, ceiling])``.
    * ``suggested`` = the largest power of two ``<= max`` (a clean, runtime-friendly size),
      lifted to 8192 when 8192 still fits — a comfortable working context.

    The ceiling is the model's **trained** maximum context (``model_max``) when known — so a
    long-context model on a roomy GPU is no longer clipped to 32k. When it is unknown, fall
    back to ``cap`` (default 32768; the caller passes the lower ``_NO_GPU_MAX_CONTEXT`` for
    CPU inference, where a huge context is impractical regardless of what the model trained
    on). With no memory figure at all, fall back to a safe minimum so the UI still has a
    value.
    """
    if not vram_total_mb or vram_total_mb <= 0:
        return SuggestedContext(min=_MIN_CONTEXT, suggested=_MIN_CONTEXT, max=_MIN_CONTEXT)

    # The trained length is the true ceiling when known; else the conservative fallback cap.
    ceiling = max(_MIN_CONTEXT, model_max if model_max else cap)
    model_mb = model_size_mb or 0
    available = vram_total_mb - model_mb - _HEADROOM_MB
    if available <= 0:
        # The model barely fits (or doesn't) — there's no room for a generous cache.
        return SuggestedContext(min=_MIN_CONTEXT, suggested=_MIN_CONTEXT, max=_MIN_CONTEXT)

    per_token = _kv_per_token_mb(model_mb) * _kv_cache_factor(kv_cache_type)
    raw_max = available / per_token
    max_ctx = max(_MIN_CONTEXT, min(ceiling, int(raw_max)))

    suggested = _next_power_of_two_at_most(max_ctx)
    # Prefer a comfortable 8K working context when it genuinely fits under the max.
    if max_ctx >= _PREFERRED_SUGGESTED:
        suggested = max(suggested, _PREFERRED_SUGGESTED)
    suggested = min(suggested, max_ctx)
    return SuggestedContext(min=_MIN_CONTEXT, suggested=suggested, max=max_ctx)


# ── GPU detectors (each best-effort; returns None on ANY failure) ──────────────────


def _detect_nvidia(runner: Runner) -> GpuInfo | None:
    """Detect an NVIDIA GPU via ``nvidia-smi`` (the only vendor exercised on the box)."""
    try:
        out = runner(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ]
        )
        first = next((line for line in out.splitlines() if line.strip()), "")
        if not first:
            return None
        parts = [p.strip() for p in first.split(",")]
        name = parts[0]
        total = int(float(parts[1]))  # MiB (nounits)
        free = int(float(parts[2])) if len(parts) > 2 and parts[2] else None
        return GpuInfo(vendor="nvidia", name=name, vram_total_mb=total, vram_free_mb=free)
    except Exception:  # missing binary, parse error, no GPU — degrade to "not found"
        log.debug("nvidia-smi detection failed", exc_info=True)
        return None


def _read_vram_from_drm(reader: FileReader, globber: Globber) -> int | None:
    """Read total VRAM (MB) from ``/sys/class/drm/card*/device/mem_info_vram_total``.

    The DRM file is in bytes. Present for discrete AMD/Intel cards with the right kernel
    driver; absent for integrated graphics (then there's nothing to report).
    """
    try:
        cards = sorted(globber("/sys/class/drm/card*/device/mem_info_vram_total"))
        for path in cards:
            try:
                raw = reader(path).strip()
                if not raw:
                    continue
                vram_bytes = int(raw)
                if vram_bytes > 0:
                    return vram_bytes // (1024 * 1024)
            except (OSError, ValueError):
                continue
        return None
    except Exception:  # globbing itself failed (non-Linux, no /sys) — nothing to report
        log.debug("drm vram read failed", exc_info=True)
        return None


def _detect_amd(runner: Runner, reader: FileReader, globber: Globber) -> GpuInfo | None:
    """Detect an AMD GPU via ``rocm-smi`` JSON, falling back to the DRM VRAM file.

    Untested on the target box (NVIDIA only). Written defensively: any failure → ``None``,
    so a box without ROCm or the right ``/dev`` mounts simply reports no AMD GPU.
    """
    try:
        out = runner(["rocm-smi", "--showmeminfo", "vram", "--json"])
        import json

        data = json.loads(out)
        for card_key, fields in data.items():
            if not isinstance(fields, dict):
                continue
            total_bytes = None
            for key, value in fields.items():
                if "vram" in key.lower() and "total" in key.lower():
                    try:
                        total_bytes = int(float(value))
                    except (TypeError, ValueError):
                        total_bytes = None
            if total_bytes:
                return GpuInfo(
                    vendor="amd",
                    name=str(fields.get("Card series") or fields.get("name") or card_key),
                    vram_total_mb=total_bytes // (1024 * 1024),
                )
    except Exception:  # rocm-smi missing or unparseable — try the kernel DRM file instead
        log.debug("rocm-smi detection failed; trying DRM", exc_info=True)

    vram_mb = _read_vram_from_drm(reader, globber)
    if vram_mb:
        return GpuInfo(vendor="amd", name="AMD GPU", vram_total_mb=vram_mb)
    return None


def _detect_intel(reader: FileReader, globber: Globber) -> GpuInfo | None:
    """Detect a discrete Intel GPU via the DRM VRAM file (untested; NVIDIA-only box).

    Integrated Intel graphics share system RAM and expose no ``mem_info_vram_total``, so
    this returns ``None`` there — the suggestion then falls back to system RAM upstream.
    """
    vram_mb = _read_vram_from_drm(reader, globber)
    if vram_mb:
        return GpuInfo(vendor="intel", name="Intel GPU", vram_total_mb=vram_mb)
    return None


def detect_gpu(
    *,
    runner: Runner = _default_runner,
    reader: FileReader = _default_file_reader,
    globber: Globber = _default_globber,
) -> GpuInfo | None:
    """Best-effort multi-vendor GPU probe: NVIDIA, then AMD, then Intel; else ``None``.

    Each detector is independently guarded (it returns ``None`` on any failure), so the
    chain never raises and "no GPU detected" (integrated graphics or none) is a normal
    result. NVIDIA is preferred because it is the only vendor exercised on the target box.
    """
    return (
        _detect_nvidia(runner)
        or _detect_amd(runner, reader, globber)
        or _detect_intel(reader, globber)
    )


def read_ram_total_mb(reader: FileReader = _default_file_reader) -> int | None:
    """Total system RAM in MB, parsed from ``/proc/meminfo`` ``MemTotal`` (kB)."""
    try:
        for line in reader("/proc/meminfo").splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])  # "MemTotal:   16327584 kB"
                return kb // 1024
        return None
    except Exception:  # non-Linux, unreadable — RAM is simply unknown
        log.debug("meminfo read failed", exc_info=True)
        return None


def read_cpu_info(reader: FileReader = _default_file_reader) -> CpuInfo | None:
    """The host CPU model + core counts, parsed from ``/proc/cpuinfo`` (Linux, in-container).

    Logical cores = number of ``processor`` entries; physical cores = ``cpu cores`` times the
    number of distinct ``physical id``s (sockets). Best-effort: on a non-Linux host (no
    ``/proc``) it still reports a logical count from ``os.cpu_count()`` when available, else
    ``None`` and the spec panel simply omits the CPU.
    """
    try:
        text = reader("/proc/cpuinfo")
    except Exception:  # non-Linux / no /proc — fall back to a bare logical count
        log.debug("cpuinfo read failed", exc_info=True)
        logical = os.cpu_count()
        return CpuInfo(model="CPU", logical_cores=logical) if logical else None

    model = "CPU"
    logical = 0
    cpu_cores: int | None = None
    physical_ids: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "model name" and value:
            model = value
        elif key == "processor":
            logical += 1
        elif key == "cpu cores" and value.isdigit():
            cpu_cores = int(value)
        elif key == "physical id":
            physical_ids.add(value)
    physical_cores = cpu_cores * (len(physical_ids) or 1) if cpu_cores else None
    return CpuInfo(
        model=model, physical_cores=physical_cores, logical_cores=logical or os.cpu_count()
    )


class _ModelSource(Protocol):
    """Minimal protocol the probe needs from the gateway.

    Structurally satisfied by :class:`~epicurus_core_app.llm.gateway.LlmGateway` (and by the
    test fakes) — the probe depends on these methods, not the whole gateway: list local
    models, resolve the active one, read a model's trained context length + quantization
    (``show``), and read the operator's KV-cache type so the suggestion can account for it.
    """

    async def models(self, tenant_id: str | None = None) -> list[ModelInfo]: ...
    async def effective_default(self, tenant_id: str | None = None) -> str: ...
    async def show(self, model: str) -> ModelDetails: ...
    async def effective_kv_cache_type(self, tenant_id: str | None = None) -> str | None: ...


async def collect_system_info(
    gateway: _ModelSource,
    *,
    tenant_id: str | None = None,
    detect: Callable[[], GpuInfo | None] = detect_gpu,
    ram: Callable[[], int | None] = read_ram_total_mb,
    cpu: Callable[[], CpuInfo | None] = read_cpu_info,
) -> SystemInfo:
    """Assemble the :class:`SystemInfo` snapshot: host spec + the context-window suggestion.

    GPU / CPU / RAM detection are injectable (the route passes the real probes; tests pass
    fakes). The model size comes from the gateway's local-model listing matched to the
    effective chat model. Memory for the suggestion is GPU VRAM when present, else system RAM.
    """
    gpu = _safe(detect, "gpu detection")
    cpu_info = _safe(cpu, "cpu detection")
    ram_total = _safe(ram, "ram detection")
    model = await _effective_model_size(gateway, tenant_id)
    kv_cache_type = await _effective_kv_cache_type(gateway, tenant_id)

    model_size = model.size_mb if model else None
    # The model's trained length is the real ceiling for the suggestion (replacing the flat
    # 32k); only applied on the GPU path — CPU inference keeps its lower hard cap regardless.
    model_max = model.context_length if model else None
    suggested = None
    if gpu is not None:
        suggested = suggest_context(
            gpu.vram_total_mb, model_size, kv_cache_type=kv_cache_type, model_max=model_max
        )
    elif ram_total:
        # No GPU: base the estimate on system RAM, with a conservative cap — CPU inference
        # with a huge context is slow and RAM is shared with the rest of the box.
        suggested = suggest_context(
            ram_total, model_size, cap=_NO_GPU_MAX_CONTEXT, kv_cache_type=kv_cache_type
        )

    return SystemInfo(
        gpu=gpu,
        cpu=cpu_info,
        ram_total_mb=ram_total,
        model=model,
        suggested_context=suggested,
        kv_cache_type=kv_cache_type,
    )


_T = TypeVar("_T")


def _safe(fn: Callable[[], _T | None], label: str) -> _T | None:
    """Run a probe, swallowing any failure (returns ``None``)."""
    try:
        return fn()
    except Exception:  # every probe is best-effort; the page renders regardless
        log.warning("%s failed", label, exc_info=True)
        return None


async def _effective_model_size(gateway: _ModelSource, tenant_id: str | None) -> ModelSize | None:
    """The active chat model and its on-disk size (MB), from the gateway's model list."""
    try:
        active = await gateway.effective_default(tenant_id)
    except Exception:
        log.warning("could not resolve the effective model", exc_info=True)
        return None
    try:
        models = await gateway.models(tenant_id)
    except Exception:  # runtime unreachable — name without a size is still useful
        log.warning("could not list local models for sizing", exc_info=True)
        return ModelSize(name=active)

    bare = active.split("/", 1)[-1]  # hosted ids carry a provider prefix; locals don't
    for info in models:
        if info.name in (active, bare) or info.name.split(":", 1)[0] == bare:
            size_mb = info.size // (1024 * 1024) if info.size else None
            # /api/show gives the trained context length (the suggestion's real ceiling) and
            # the weight quantization. Best-effort — it degrades to None on any failure.
            details = await _safe_show(gateway, info.name)
            return ModelSize(
                name=info.name,
                size_mb=size_mb,
                context_length=details.context_length,
                quantization=details.quantization,
            )
    # The active model isn't a local one (e.g. a hosted provider): no on-disk size.
    return ModelSize(name=active)


async def _safe_show(gateway: _ModelSource, model: str) -> ModelDetails:
    """``gateway.show`` wrapped so any failure degrades to empty details (all ``None``)."""
    try:
        return await gateway.show(model)
    except Exception:  # runtime unreachable / unexpected shape — the facts are simply unknown
        log.warning("could not read model details for sizing", exc_info=True)
        return ModelDetails()


async def _effective_kv_cache_type(gateway: _ModelSource, tenant_id: str | None) -> str | None:
    """The operator's KV-cache type, best-effort (``None`` on any failure → f16 baseline)."""
    try:
        return await gateway.effective_kv_cache_type(tenant_id)
    except Exception:  # prefs store hiccup — the suggestion just assumes the f16 baseline
        log.warning("could not read the KV-cache type", exc_info=True)
        return None


def create_system_router(
    gateway: _ModelSource,
    *,
    detect: Callable[[], GpuInfo | None] = detect_gpu,
    ram: Callable[[], int | None] = read_ram_total_mb,
    cpu: Callable[[], CpuInfo | None] = read_cpu_info,
) -> APIRouter:
    """The ``GET /platform/v1/system/info`` route backing the spec panel + suggestion."""
    router = APIRouter(prefix="/platform/v1/system", tags=["system"])

    @router.get("/info", response_model=SystemInfo)
    async def system_info() -> SystemInfo:
        return await collect_system_info(gateway, detect=detect, ram=ram, cpu=cpu)

    return router
