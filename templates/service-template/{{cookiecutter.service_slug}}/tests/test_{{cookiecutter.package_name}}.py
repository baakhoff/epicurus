"""Tests for the {{ cookiecutter.service_name }} module."""

from __future__ import annotations

from {{ cookiecutter.package_name }}.service import build_module


async def test_module_builds() -> None:
    manifest = await build_module().manifest()
    assert manifest.name == "{{ cookiecutter.service_slug }}"
    assert any(t.name == "ping" for t in manifest.tools)


async def test_ping_tool() -> None:
    _, structured = await build_module().mcp.call_tool("ping", {"message": "hi"})
    assert structured == {"result": "{{ cookiecutter.service_slug }}: hi"}
