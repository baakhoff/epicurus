"""Whether this installation can actually re-embed what an import just landed (#893).

The last thing an apply does is ask every reindexable module to rebuild its vectors (#332).
That fan-out is fire-and-forget by design — each module answers "started" and does the work
on its own time — which means the one condition that makes it *pointless* is also the one it
cannot report: **there is no embedding model here**. On a fresh install the rows and files
land, every module accepts the re-embed, each one retries against a model the runtime has
never heard of, and minutes later they are all parked in ``error`` with nothing in the import
report but "re-embed asked of 7 module(s)".

So the core asks the question before it asks the modules, and writes the answer into the
report. Two shapes of "no":

* a **local** model that is not pulled (the common one — a new box has Ollama and no models),
  or a runtime that cannot be reached to say;
* a **hosted** model whose provider has no API key in OpenBao (or a vault that will not
  answer, which is *not* the same fact and must not be reported as one — #728).

The sentence is written here, not in the shell: the card renders data, not decisions
(ADR-0018), and "pull it on Models, then run Re-embed everything" is a fact about this
deployment that only the core is in a position to state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from epicurus_core import get_logger
from epicurus_core_app.llm import providers as registry

__all__ = ["EmbeddingProbe", "EmbeddingStatus", "embedding_status"]

log = get_logger("core.portability.embedding")

_REMEDY = "Pull one on Models, then run “Re-embed everything” — the imported data is unharmed."


class EmbeddingStatus(NamedTuple):
    """The embedding model a re-embed would use, and why it would not work.

    ``note`` is ``None`` when the fan-out can succeed. It is deliberately a sentence rather
    than a code: it is carried verbatim into the import report and rendered verbatim by the
    card, so it has to read as something an operator can act on.
    """

    model: str | None
    note: str | None


class EmbeddingProbe(Protocol):
    """The slice of :class:`~epicurus_core_app.llm.gateway.LlmGateway` this needs.

    Structural, like :class:`~epicurus_core_app.portability.service.ModuleTargets`: the
    gateway satisfies it without knowing portability exists, and a test stands in three
    methods instead of a whole LLM stack.
    """

    async def effective_embed_default(self, tenant_id: str | None = None) -> str: ...

    async def models(
        self, tenant_id: str | None = None, *, with_capabilities: bool = False
    ) -> Sequence[Any]: ...

    async def providers(self, tenant_id: str | None = None) -> Sequence[Any]: ...


async def embedding_status(probe: EmbeddingProbe, *, tenant: str) -> EmbeddingStatus:
    """Resolve the tenant's embedding model and say whether this install can serve it.

    Never raises. A probe that itself fails is reported as "could not check" rather than as
    "missing": an import that landed cleanly must not be described as broken because the
    model runtime happened to be restarting, and a false "no embedding model" would send the
    operator hunting for a model they already have (the #728 lesson, applied here).
    """
    try:
        model = await probe.effective_embed_default(tenant)
    except Exception as exc:
        log.warning("embedding model could not be resolved", error=str(exc))
        return EmbeddingStatus(None, f"The embedding model could not be resolved: {exc}. {_REMEDY}")
    if registry.is_hosted(model):
        return EmbeddingStatus(model, await _hosted_note(probe, model, tenant))
    return EmbeddingStatus(model, await _local_note(probe, model, tenant))


async def _local_note(probe: EmbeddingProbe, model: str, tenant: str) -> str | None:
    """Whether the local runtime has *model* pulled, in the operator's words."""
    try:
        installed = [str(getattr(info, "name", "")) for info in await probe.models(tenant)]
    except Exception as exc:
        log.warning("embedding model probe failed", model=model, error=str(exc))
        return (
            f"The local model runtime could not be reached, so whether the embedding model "
            f"“{model}” is installed is unknown ({type(exc).__name__}). If the re-embed comes "
            f"back empty, check Models and run “Re-embed everything”."
        )
    # The runtime tags what it holds (``nomic-embed-text:latest``) while a pref may name it
    # bare, or the other way round — the same loose match ``model_readiness`` makes.
    wanted = model.split("/", 1)[-1]
    family = wanted.split(":", 1)[0]
    if any(name == wanted or name.split(":", 1)[0] == family for name in installed):
        return None
    return (
        f"No embedding model is installed here: “{model}” is not among the "
        f"{len(installed)} model(s) the local runtime holds, so every module's re-embed will "
        f"fail. {_REMEDY}"
    )


async def _hosted_note(probe: EmbeddingProbe, model: str, tenant: str) -> str | None:
    """Whether the hosted provider behind *model* has a key this tenant can use."""
    alias = model.partition("/")[0]
    try:
        infos = await probe.providers(tenant)
    except Exception as exc:
        log.warning("embedding provider probe failed", model=model, error=str(exc))
        return (
            f"Whether the provider “{alias}” behind the embedding model “{model}” has a key "
            f"here could not be checked ({type(exc).__name__}). If the re-embed comes back "
            f"empty, re-enter the key on Models and run “Re-embed everything”."
        )
    state = next(
        (
            str(getattr(info, "key_state", ""))
            for info in infos
            if getattr(info, "alias", "") == alias
        ),
        "missing",
    )
    if state == "present":
        return None
    if state == "unavailable":
        return (
            f"The vault did not answer, so whether “{alias}” holds the key for the embedding "
            f"model “{model}” is unknown. If the re-embed comes back empty, check the key on "
            f"Models and run “Re-embed everything”."
        )
    return (
        f"The embedding model “{model}” is hosted by “{alias}”, which has no API key here, so "
        f"every module's re-embed will fail. Enter the key on Models, then run "
        f"“Re-embed everything” — the imported data is unharmed."
    )
