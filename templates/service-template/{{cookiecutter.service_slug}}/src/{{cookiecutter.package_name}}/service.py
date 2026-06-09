"""The {{ cookiecutter.service_name }} module: its tools and declared events.

Built on `epicurus-core`. Replace the sample `ping` tool with the module's real
capability, and declare any NATS events it emits/consumes with `module.emits(...)`
/ `module.consumes(...)`.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule

MODULE_NAME = "{{ cookiecutter.service_slug }}"


def build_module() -> EpicurusModule:
    """Build the {{ cookiecutter.service_name }} module and register its tools/events."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
        description="{{ cookiecutter.description }}",
    )

    @module.tool()
    def ping(message: str = "hello") -> str:
        """A sample tool — replace with the module's real capability."""
        return f"{MODULE_NAME}: {message}"

    return module
