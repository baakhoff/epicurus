"""The autonomy dial, enforced at the real tool surface.

The dial's whole claim is that a level's allowance is *structural*, not persuasive: a
Notify automation is not asked to avoid writing, it is handed no tool that can. That claim
lives in ``McpHost.discover(allow=…)``, so these tests drive the real thing.

The distinction they exist to defend: ``discover`` returns ``(specs, route)``. ``specs`` is
only what the model is *told about*; ``route`` is what ``call`` will actually dispatch.
Filtering specs alone would leave a dial a determined model can talk its way past — so
every test here asserts on **both**, and the important assertions are the ``route`` ones.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from epicurus_core import SideEffect
from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.agent.mcp_host import McpHost
from epicurus_core_app.automations.model import allowed_side_effects

# The classification the registry would resolve from the modules' manifests. Real tool
# names on purpose — including mail_mark_read, which contains "read" and mutates.
_CLASSES: dict[str, SideEffect] = {
    "mail_search": "read",
    "mail_send": "propose",  # composes a draft for review; cannot transmit
    "mail_mark_read": "write",  # reads nothing — it mutates. The naming trap.
}


def _spec(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


async def _host(*, classes: dict[str, SideEffect] | None = None) -> McpHost:
    """A host whose only tools are built-ins, so discovery needs no MCP server.

    Built-ins take the same ``allow`` path as module tools (the same ``discover`` call
    filters both), so this exercises the real filter without standing up a module.
    """
    host = McpHost([])
    if classes is not None:
        host.set_side_effect_provider(_provider(classes))
    return host


def _provider(classes: dict[str, SideEffect]):
    async def _get() -> dict[str, SideEffect]:
        return classes

    return _get


async def _handler(_args: dict[str, Any], _tenant: str) -> str:
    return "ok"


def _names(specs: list[dict[str, Any]]) -> set[str]:
    return {s["function"]["name"] for s in specs}


# ── built-ins: the dial filters what the model sees AND what it can reach ────


async def _host_with_builtins() -> McpHost:
    host = await _host()
    host.register_builtin("reader", _spec("reader"), _handler, side_effect="read")
    host.register_builtin("proposer", _spec("proposer"), _handler, side_effect="propose")
    host.register_builtin("writer", _spec("writer"), _handler, side_effect="write")
    return host


async def test_no_allow_offers_everything() -> None:
    # An ordinary chat turn passes allow=None and is unaffected by the dial.
    host = await _host_with_builtins()
    specs, route = await host.discover()
    assert _names(specs) == {"reader", "proposer", "writer"}
    assert set(route) == {"reader", "proposer", "writer"}


async def test_notify_reaches_only_read_tools() -> None:
    host = await _host_with_builtins()
    specs, route = await host.discover(allow=allowed_side_effects("notify"))
    assert _names(specs) == {"reader"}
    # The one that matters: a withheld tool is *unroutable*, not merely unmentioned.
    assert set(route) == {"reader"}


async def test_propose_reaches_read_and_propose_but_not_write() -> None:
    host = await _host_with_builtins()
    specs, route = await host.discover(allow=allowed_side_effects("propose"))
    assert _names(specs) == {"reader", "proposer"}
    assert set(route) == {"reader", "proposer"}


async def test_act_reaches_everything() -> None:
    host = await _host_with_builtins()
    _specs, route = await host.discover(allow=allowed_side_effects("act"))
    assert set(route) == {"reader", "proposer", "writer"}


async def test_silent_act_reaches_exactly_what_act_does() -> None:
    host = await _host_with_builtins()
    _s1, act_route = await host.discover(allow=allowed_side_effects("act"))
    _s2, silent_route = await host.discover(allow=allowed_side_effects("silent_act"))
    assert set(act_route) == set(silent_route)


async def test_a_builtin_defaults_to_write() -> None:
    # register_builtin's default is the restrictive one, so a built-in added later is
    # withheld from a read-only automation until someone states otherwise.
    host = await _host()
    host.register_builtin("unstated", _spec("unstated"), _handler)
    _specs, route = await host.discover(allow=allowed_side_effects("notify"))
    assert route == {}
    _specs, route = await host.discover(allow=allowed_side_effects("act"))
    assert set(route) == {"unstated"}


async def test_a_withheld_tool_is_refused_as_unknown_if_the_model_asks_anyway() -> None:
    # The refusal, end to end. `Agent._invoke` dispatches on `route`, so a withheld tool
    # is not merely unmentioned — a model that names it anyway is told it does not exist,
    # and nothing runs. This is the difference between a dial and a polite request.
    host = await _host_with_builtins()
    _specs, route = await host.discover(allow=allowed_side_effects("notify"))
    agent = Agent(gateway=MagicMock(), mcp=host)
    text, is_error = await agent._invoke("writer", {}, route, tenant="local")
    assert is_error is True
    assert "unknown tool" in text


async def test_an_allowed_tool_still_dispatches() -> None:
    host = await _host_with_builtins()
    _specs, route = await host.discover(allow=allowed_side_effects("notify"))
    agent = Agent(gateway=MagicMock(), mcp=host)
    text, is_error = await agent._invoke("reader", {}, route, tenant="local")
    assert is_error is False
    assert text == "ok"


# ── the classification provider ──────────────────────────────────────────────


async def test_the_provider_is_only_consulted_when_a_dial_is_applied() -> None:
    # An ordinary turn must not pay for a registry snapshot it will not use.
    calls = 0

    async def _counting() -> dict[str, SideEffect]:
        nonlocal calls
        calls += 1
        return dict(_CLASSES)

    host = await _host()
    host.register_builtin("reader", _spec("reader"), _handler, side_effect="read")
    host.set_side_effect_provider(_counting)

    await host.discover()
    assert calls == 0
    await host.discover(allow=allowed_side_effects("notify"))
    assert calls == 1


# ── module tools: the same filter, over the real discovery path ─────────────


def _mock_transport(tool_names: list[str]) -> tuple[object, object]:
    """(transport_cm, session_cm) mocks advertising *tool_names* — as test_mcp_host does."""
    tools = []
    for name in tool_names:
        tool = MagicMock()
        tool.name = name
        tool.description = ""
        tool.inputSchema = {}
        tools.append(tool)
    listing = MagicMock()
    listing.tools = tools

    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=listing)

    transport_cm = MagicMock()
    transport_cm.__aenter__ = AsyncMock(return_value=(None, None, None))
    transport_cm.__aexit__ = AsyncMock(return_value=False)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return transport_cm, session_cm


async def _discover_module_tools(
    tool_names: list[str],
    classes: dict[str, SideEffect],
    level: str,
) -> tuple[set[str], set[str]]:
    """Discover *tool_names* from a fake module at *level*; returns (spec names, routes)."""
    host = McpHost(["http://a:8080/mcp"])
    host.set_side_effect_provider(_provider(classes))
    transport_cm, session_cm = _mock_transport(tool_names)
    with (
        patch("epicurus_core_app.agent.mcp_host.streamablehttp_client", return_value=transport_cm),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
    ):
        specs, route = await host.discover(allow=allowed_side_effects(level))  # type: ignore[arg-type]
    return _names(specs), set(route)


async def test_module_tools_are_filtered_by_their_declared_class() -> None:
    names, route = await _discover_module_tools(list(_CLASSES), _CLASSES, "notify")
    assert names == {"mail_search"}
    assert route == {"mail_search"}


async def test_an_unclassified_module_tool_is_withheld_from_notify() -> None:
    # Fail closed, over the real discovery path. A module that never declared a side
    # effect must not have its tools trusted by a read-only automation: the cost of a
    # forgotten annotation is availability, never containment.
    names, route = await _discover_module_tools(["mystery"], {}, "notify")
    assert names == set()
    assert route == set()


async def test_an_unclassified_module_tool_is_available_at_act() -> None:
    # The other half of failing closed: it is treated as a *write*, not as forbidden —
    # so an un-annotated module still works for an automation that may write.
    names, route = await _discover_module_tools(["mystery"], {}, "act")
    assert names == {"mystery"}
    assert route == {"mystery"}


async def test_the_disabled_filter_and_the_dial_compose() -> None:
    # A tool the operator turned off (#213) stays off regardless of the level — the dial
    # widens what an automation may reach, never what the operator forbade.
    host = McpHost(["http://a:8080/mcp"])
    host.set_side_effect_provider(_provider(_CLASSES))

    async def _disabled() -> set[str]:
        return {"mail_search"}

    host.set_tool_filter(_disabled)
    transport_cm, session_cm = _mock_transport(["mail_search", "mail_mark_read"])
    with (
        patch("epicurus_core_app.agent.mcp_host.streamablehttp_client", return_value=transport_cm),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
    ):
        _specs, route = await host.discover(allow=allowed_side_effects("act"))
    assert set(route) == {"mail_mark_read"}  # the disabled read tool stays gone


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("notify", {"mail_search"}),
        ("propose", {"mail_search", "mail_send"}),
        ("act", {"mail_search", "mail_send", "mail_mark_read"}),
    ],
)
def test_the_dial_against_realistic_tool_names(level: str, expected: set[str]) -> None:
    """A sanity check on the vocabulary using real tools, including the naming trap.

    ``mail_mark_read`` contains "read" and mutates — which is exactly why the
    classification is declared rather than inferred from a name.
    """
    allow = allowed_side_effects(level)  # type: ignore[arg-type]
    reachable = {name for name, cls in _CLASSES.items() if cls in allow}
    assert reachable == expected
