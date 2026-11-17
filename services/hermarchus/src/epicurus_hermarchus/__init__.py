"""Hermarchus — the agent host.

One instance is bound to exactly one profile (a tenant id) and runs any number of agent
sessions side by side on one workspace filesystem. A *controller* holding the instance's
bearer token drives it over HTTP: it creates sessions, sends input through a durable inbox,
reads each session's ordered event log as server-sent events (with replay), and answers the
permission requests agents raise. Agent programs are spoken to over the Agent Client
Protocol on stdio; a built-in ``echo`` agent is the reference implementation.
"""

from __future__ import annotations

__version__ = "0.1.0"

# The controller <-> Hermarchus contract version, reported by ``GET /v1/info``. It is the
# major of the ``/v1`` path prefix: a breaking change to a shape bumps both.
CONTRACT_VERSION = 1

__all__ = ["CONTRACT_VERSION", "__version__"]
