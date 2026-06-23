"""Unit tests for the system/GPU probe and the context-window suggestion.

GPU detectors, RAM parsing, and the model lister are all injectable, so these tests feed
canned ``nvidia-smi`` / ``rocm-smi`` output and fake ``/sys`` + ``/proc`` reads — no real
hardware is touched. The box this ships to has only NVIDIA, so the AMD/Intel paths are
validated here against synthetic output rather than live cards.
"""

from __future__ import annotations

from epicurus_core_app.llm.models import ModelInfo
from epicurus_core_app.system_info import (
    GpuInfo,
    ModelSize,
    SystemInfo,
    collect_system_info,
    detect_gpu,
    read_ram_total_mb,
    suggest_context,
)

# ── suggest_context (pure) ────────────────────────────────────────────────────────


def test_suggest_context_no_memory_returns_minimum() -> None:
    s = suggest_context(None, 4700)
    assert s.min == s.suggested == s.max == 2048


def test_suggest_context_large_gpu_gives_a_generous_range() -> None:
    # 24 GB VRAM, ~4.7 GB model: plenty of room → a big max, suggested a power of two ≥ 8192.
    s = suggest_context(24576, 4700)
    assert s.min == 2048
    assert s.max == 32768  # clamped at the ceiling
    assert s.suggested >= 8192
    assert s.suggested <= s.max
    # suggested is a power of two (clean runtime size)
    assert (s.suggested & (s.suggested - 1)) == 0


def test_suggest_context_small_gpu_is_constrained() -> None:
    # 6 GB VRAM with a ~4.7 GB model leaves little for the KV cache → a modest max.
    s = suggest_context(6144, 4700)
    assert s.min == 2048
    assert 2048 <= s.suggested <= s.max <= 32768


def test_suggest_context_model_too_big_falls_to_minimum() -> None:
    # The model alone exceeds VRAM → nothing left for context; fall back to the safe min.
    s = suggest_context(4096, 8000)
    assert s.min == s.suggested == s.max == 2048


def test_suggest_context_scales_with_available_memory() -> None:
    small = suggest_context(8192, 3000)
    large = suggest_context(49152, 3000)
    # More VRAM (same model) must never suggest a *smaller* maximum.
    assert large.max >= small.max


def test_suggest_context_suggested_never_exceeds_max() -> None:
    for vram in (5000, 7000, 9000, 12000, 16000):
        s = suggest_context(vram, 4700)
        assert s.suggested <= s.max
        assert s.suggested >= s.min


def test_suggest_context_honors_a_lower_cap_for_no_gpu() -> None:
    # The no-GPU path passes a conservative cap (system RAM, CPU inference). Lots of RAM but
    # the max must not exceed the cap.
    s = suggest_context(64000, 4700, cap=8192)
    assert s.max <= 8192
    assert s.suggested <= 8192


# ── NVIDIA detection ──────────────────────────────────────────────────────────────


def test_detect_nvidia_parses_smi_output() -> None:
    def runner(argv: list[str]) -> str:
        assert argv[0] == "nvidia-smi"
        return "NVIDIA GeForce RTX 4090, 24564, 23000\n"

    gpu = detect_gpu(runner=runner, reader=_no_file, globber=_no_glob)
    assert gpu == GpuInfo(
        vendor="nvidia", name="NVIDIA GeForce RTX 4090", vram_total_mb=24564, vram_free_mb=23000
    )


def test_detect_nvidia_first_of_multiple_gpus() -> None:
    def runner(argv: list[str]) -> str:
        return "GPU A, 8192, 8000\nGPU B, 16384, 16000\n"

    gpu = detect_gpu(runner=runner, reader=_no_file, globber=_no_glob)
    assert gpu is not None and gpu.name == "GPU A" and gpu.vram_total_mb == 8192


def test_detect_nvidia_missing_binary_degrades_to_none() -> None:
    def runner(argv: list[str]) -> str:
        raise FileNotFoundError("nvidia-smi not found")

    # No NVIDIA, no DRM files → no GPU at all (a normal result, not an error).
    assert detect_gpu(runner=runner, reader=_no_file, globber=_no_glob) is None


def test_detect_nvidia_garbage_output_does_not_raise() -> None:
    def runner(argv: list[str]) -> str:
        return "not,really,numbers\n"

    assert detect_gpu(runner=runner, reader=_no_file, globber=_no_glob) is None


# ── AMD / Intel via the DRM VRAM file (untested hardware — synthetic) ──────────────


def test_detect_amd_via_drm_when_rocm_missing() -> None:
    def runner(argv: list[str]) -> str:
        # nvidia-smi absent, rocm-smi absent → fall through to the DRM file.
        raise FileNotFoundError(argv[0])

    def reader(path: str) -> str:
        assert path == "/sys/class/drm/card0/device/mem_info_vram_total"
        return str(16 * 1024 * 1024 * 1024)  # 16 GiB, in bytes

    def globber(pattern: str) -> list[str]:
        return ["/sys/class/drm/card0/device/mem_info_vram_total"]

    gpu = detect_gpu(runner=runner, reader=reader, globber=globber)
    assert gpu is not None
    assert gpu.vendor == "amd"  # AMD is tried before Intel; both read the same DRM file
    assert gpu.vram_total_mb == 16384


def test_detect_amd_from_rocm_json() -> None:
    def runner(argv: list[str]) -> str:
        if argv[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        # rocm-smi --showmeminfo vram --json
        return (
            '{"card0": {"Card series": "Radeon RX 7900 XTX", '
            '"VRAM Total Memory (B)": "25753026560"}}'
        )

    gpu = detect_gpu(runner=runner, reader=_no_file, globber=_no_glob)
    assert gpu is not None
    assert gpu.vendor == "amd"
    assert gpu.name == "Radeon RX 7900 XTX"
    assert gpu.vram_total_mb == 25753026560 // (1024 * 1024)


def test_no_gpu_when_nothing_detects() -> None:
    def runner(argv: list[str]) -> str:
        raise FileNotFoundError(argv[0])

    assert detect_gpu(runner=runner, reader=_no_file, globber=_no_glob) is None


# ── RAM ────────────────────────────────────────────────────────────────────────────


def test_read_ram_total_parses_meminfo() -> None:
    def reader(path: str) -> str:
        assert path == "/proc/meminfo"
        return "MemTotal:       16327584 kB\nMemFree:         1000000 kB\n"

    assert read_ram_total_mb(reader) == 16327584 // 1024


def test_read_ram_total_missing_file_returns_none() -> None:
    def reader(path: str) -> str:
        raise FileNotFoundError(path)

    assert read_ram_total_mb(reader) is None


# ── collect_system_info (assembly) ────────────────────────────────────────────────


class _FakeGateway:
    """A stand-in exposing just the two methods the probe needs."""

    def __init__(self, *, default: str, models: list[ModelInfo]) -> None:
        self._default = default
        self._models = models

    async def effective_default(self, tenant_id: str | None = None) -> str:
        return self._default

    async def models(self, tenant_id: str | None = None) -> list[ModelInfo]:
        return self._models


async def test_collect_uses_gpu_and_sizes_the_active_model() -> None:
    gateway = _FakeGateway(
        default="llama3.2",
        models=[
            ModelInfo(name="llama3.2:latest", size=4_700_000_000),
            ModelInfo(name="qwen2.5:0.5b", size=400_000_000),
        ],
    )
    info = await collect_system_info(
        gateway,
        detect=lambda: GpuInfo(vendor="nvidia", name="RTX 4090", vram_total_mb=24564),
        ram=lambda: 32000,
    )
    assert isinstance(info, SystemInfo)
    assert info.gpu is not None and info.gpu.vram_total_mb == 24564
    assert info.ram_total_mb == 32000
    assert info.model is not None
    assert info.model.name == "llama3.2:latest"
    assert info.model.size_mb == 4_700_000_000 // (1024 * 1024)
    # The suggestion is computed from VRAM (a GPU was present), not RAM.
    assert info.suggested_context is not None
    assert info.suggested_context.max > info.suggested_context.min


async def test_collect_falls_back_to_ram_without_a_gpu() -> None:
    gateway = _FakeGateway(
        default="llama3.2", models=[ModelInfo(name="llama3.2", size=4_700_000_000)]
    )
    info = await collect_system_info(gateway, detect=lambda: None, ram=lambda: 16000)
    assert info.gpu is None
    # No GPU → suggestion is based on RAM (still produced so the UI has a value).
    assert info.suggested_context is not None


async def test_collect_degrades_when_probes_raise() -> None:
    gateway = _FakeGateway(default="llama3.2", models=[])

    def boom_gpu() -> GpuInfo | None:
        raise RuntimeError("nvidia-smi blew up")

    def boom_ram() -> int | None:
        raise RuntimeError("/proc unreadable")

    # Every probe failing must not raise — the snapshot just has Nones.
    info = await collect_system_info(gateway, detect=boom_gpu, ram=boom_ram)
    assert info.gpu is None
    assert info.ram_total_mb is None
    assert info.suggested_context is None


async def test_collect_handles_hosted_model_with_no_local_size() -> None:
    gateway = _FakeGateway(default="claude/claude-sonnet-4-6", models=[ModelInfo(name="llama3.2")])
    info = await collect_system_info(
        gateway,
        detect=lambda: GpuInfo(vendor="nvidia", name="RTX", vram_total_mb=24564),
        ram=lambda: 32000,
    )
    assert info.model == ModelSize(name="claude/claude-sonnet-4-6", size_mb=None)
    # No model size still yields a suggestion (it just uses 0 for the model footprint).
    assert info.suggested_context is not None


# ── helpers ────────────────────────────────────────────────────────────────────────


def _no_file(path: str) -> str:
    raise FileNotFoundError(path)


def _no_glob(pattern: str) -> list[str]:
    return []


# ── read_cpu_info (injectable reader) ───────────────────────────────────────────────


def test_read_cpu_info_parses_model_and_cores() -> None:
    from epicurus_core_app.system_info import read_cpu_info

    cpuinfo = (
        "processor\t: 0\nmodel name\t: AMD Ryzen 9 5900X\ncpu cores\t: 12\nphysical id\t: 0\n\n"
        "processor\t: 1\nmodel name\t: AMD Ryzen 9 5900X\ncpu cores\t: 12\nphysical id\t: 0\n\n"
    )
    info = read_cpu_info(reader=lambda _p: cpuinfo)
    assert info is not None
    assert info.model == "AMD Ryzen 9 5900X"
    assert info.logical_cores == 2  # two "processor" blocks
    assert info.physical_cores == 12  # cpu cores * 1 socket


def test_read_cpu_info_degrades_when_proc_unreadable() -> None:
    from epicurus_core_app.system_info import read_cpu_info

    def boom(_p: str) -> str:
        raise FileNotFoundError("/proc/cpuinfo")

    info = read_cpu_info(reader=boom)
    # Non-Linux fallback: a logical count from os.cpu_count(), model "CPU" (or None if unknown).
    assert info is None or info.model == "CPU"


async def test_collect_system_info_includes_cpu() -> None:
    class _Gateway:
        async def models(self, tenant_id: str | None = None) -> list[ModelInfo]:
            return []

        async def effective_default(self, tenant_id: str | None = None) -> str:
            return "llama3.2"

    from epicurus_core_app.system_info import CpuInfo

    info = await collect_system_info(
        _Gateway(),
        detect=lambda: None,
        ram=lambda: 16000,
        cpu=lambda: CpuInfo(model="Test CPU", physical_cores=8, logical_cores=16),
    )
    assert info.cpu is not None
    assert info.cpu.model == "Test CPU"
    assert info.cpu.logical_cores == 16
