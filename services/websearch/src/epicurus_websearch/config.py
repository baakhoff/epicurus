"""The websearch module's effective settings: the operator's stored choice over env defaults.

Until #984 the Modules page's settings form for this module was stored by the core and never
read by anything — ``websearch_max_results`` / ``websearch_engines`` came from env only. The
module now reads the stored values back through the core (``ModuleConfigCache``, a short cache
over ``PlatformClient.get_module_config``) on each tool call, so a saved setting takes effect
within seconds and without a restart.

**Precedence.** A stored value wins when it *differs from the form's default*; a stored value
equal to the default — or none at all — falls back to the env value. The rule exists because
the shell's form submits every field, defaults included: an operator who saves the form only
to change the backend would otherwise silently wipe an env-configured engine list (or result
count) with the blank default they never touched. The cost is that the form cannot set a
value *back* to the default over a non-default env value; the env file is where that lives.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

Backend = Literal["searxng", "openrouter"]

BACKEND_KEY = "websearch_backend"
MAX_RESULTS_KEY = "websearch_max_results"
ENGINES_KEY = "websearch_engines"

SEARXNG: Backend = "searxng"
OPENROUTER: Backend = "openrouter"

# The form's own defaults — kept beside the schema that declares them (``service.py``).
DEFAULT_MAX_RESULTS = 5
DEFAULT_ENGINES = ""


@dataclass(frozen=True)
class EffectiveConfig:
    """What one ``web_search`` call runs with."""

    backend: Backend = SEARXNG
    max_results: int = DEFAULT_MAX_RESULTS
    # ``None`` = the SearXNG client's own list (the env value it was built with).
    engines: str | None = None


ConfigSource = Callable[[], Awaitable[EffectiveConfig]]


def resolve(stored: dict[str, Any], *, env_max_results: int, env_engines: str) -> EffectiveConfig:
    """Merge the operator's stored settings over the env defaults (see the module docstring).

    Anything malformed in ``stored`` is ignored field by field rather than failing the tool:
    an unknown backend reads as SearXNG (today's behaviour), a non-integer or out-of-range
    result count as the env value.
    """
    backend: Backend = OPENROUTER if stored.get(BACKEND_KEY) == OPENROUTER else SEARXNG

    max_results = env_max_results
    raw_max = stored.get(MAX_RESULTS_KEY)
    if (
        isinstance(raw_max, int)
        and not isinstance(raw_max, bool)
        and 1 <= raw_max <= 20
        and raw_max != DEFAULT_MAX_RESULTS
    ):
        max_results = raw_max

    engines = env_engines
    raw_engines = stored.get(ENGINES_KEY)
    if isinstance(raw_engines, str) and raw_engines.strip() != DEFAULT_ENGINES:
        engines = raw_engines.strip()

    return EffectiveConfig(backend=backend, max_results=max_results, engines=engines)


def static_source(config: EffectiveConfig) -> ConfigSource:
    """A source that always answers ``config`` — for tests and a module built without a core."""

    async def source() -> EffectiveConfig:
        return config

    return source
