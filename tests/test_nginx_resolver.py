"""The web image derives nginx's resolver from the container's own DNS (#891).

``/platform/`` resolves the core at request time, so the config must name a resolver — and the
literal that used to be there (``127.0.0.11``, Docker's embedded DNS) does not exist in a
Kubernetes pod, where every proxied request would fail at name resolution. The image now
derives it at container start from ``/etc/resolv.conf``.

That derivation is a shell fragment sourced by the nginx entrypoint, which no other gate can
see: `runtime-smoke` proves the Docker path end-to-end (a wrong value stops nginx from
starting at all), but nothing proves the *cluster* path, and nobody boots a pod to find out
that an IPv6 nameserver was written unbracketed. So the script is exercised here directly,
against fixture resolv.conf files, exactly as the entrypoint sources it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "services" / "web"
SCRIPT = WEB / "docker-entrypoint.d" / "05-epicurus-resolver.envsh"
TEMPLATE = WEB / "nginx.conf.template"
DOCKERFILE = WEB / "Dockerfile"


def _derive(resolv: str | None, *, env: dict[str, str] | None = None, tmp_path: Path) -> str:
    """Source the fragment the way the nginx entrypoint does; return ``$NGINX_RESOLVER``."""
    environment = {"PATH": "/usr/bin:/bin", **(env or {})}
    if resolv is not None:
        conf = tmp_path / "resolv.conf"
        conf.write_text(resolv, encoding="utf-8")
        environment["RESOLV_CONF"] = str(conf)
    else:
        environment["RESOLV_CONF"] = str(tmp_path / "absent.conf")
    result = subprocess.run(
        ["sh", "-c", f'. "{SCRIPT}"; printf "%s" "$NGINX_RESOLVER"'],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell available")


def test_the_docker_embedded_resolver_is_derived_unchanged(tmp_path: Path) -> None:
    """Under Compose the value must still be exactly what the template used to hardcode."""
    resolv = "search .\nnameserver 127.0.0.11\noptions ndots:0\n"
    assert _derive(resolv, tmp_path=tmp_path) == "127.0.0.11"


def test_a_cluster_dns_service_address_is_derived(tmp_path: Path) -> None:
    resolv = (
        "search epicurus.svc.cluster.local svc.cluster.local cluster.local\n"
        "nameserver 10.43.0.10\n"
        "options ndots:5\n"
    )
    assert _derive(resolv, tmp_path=tmp_path) == "10.43.0.10"


def test_the_first_nameserver_wins(tmp_path: Path) -> None:
    resolv = "nameserver 10.43.0.10\nnameserver 1.1.1.1\n"
    assert _derive(resolv, tmp_path=tmp_path) == "10.43.0.10"


def test_an_ipv6_nameserver_is_bracketed(tmp_path: Path) -> None:
    """An unbracketed IPv6 literal is a syntax error nginx refuses to start with."""
    assert _derive("nameserver fd00:10:96::a\n", tmp_path=tmp_path) == "[fd00:10:96::a]"


def test_an_already_bracketed_address_is_left_alone(tmp_path: Path) -> None:
    assert _derive("nameserver [fd00:10:96::a]\n", tmp_path=tmp_path) == "[fd00:10:96::a]"


def test_a_resolv_conf_with_no_nameserver_keeps_the_compose_default(tmp_path: Path) -> None:
    assert _derive("search .\noptions ndots:0\n", tmp_path=tmp_path) == "127.0.0.11"


def test_a_missing_resolv_conf_keeps_the_compose_default(tmp_path: Path) -> None:
    assert _derive(None, tmp_path=tmp_path) == "127.0.0.11"


def test_a_commented_out_nameserver_is_not_read(tmp_path: Path) -> None:
    resolv = "#nameserver 8.8.8.8\nnameserver 10.43.0.10\n"
    assert _derive(resolv, tmp_path=tmp_path) == "10.43.0.10"


def test_an_operator_override_wins(tmp_path: Path) -> None:
    """``NGINX_RESOLVER`` set on the container is never second-guessed."""
    derived = _derive(
        "nameserver 10.43.0.10\n", env={"NGINX_RESOLVER": "10.0.0.53"}, tmp_path=tmp_path
    )
    assert derived == "10.0.0.53"


# ── the wiring that makes the derived value reach nginx ──────────────────────────


def test_the_template_takes_its_resolver_from_the_environment() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "resolver ${NGINX_RESOLVER}" in text
    assert "resolver 127.0.0.11" not in text, (
        "a hardcoded resolver is back in the template — it does not exist in a cluster"
    )
    # Both proxy locations resolve at request time, so both need it.
    assert text.count("resolver ${NGINX_RESOLVER}") == 2


def test_envsubst_is_filtered_to_our_two_variables() -> None:
    """Without the filter, envsubst would eat nginx's own ``$host``/``$core`` variables."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "NGINX_ENVSUBST_FILTER" in dockerfile
    assert "CORE_APP_URL" in dockerfile and "NGINX_RESOLVER" in dockerfile
    template = TEMPLATE.read_text(encoding="utf-8")
    # The nginx variables the proxy blocks depend on must survive rendering.
    for variable in ("$host", "$core", "$proxy_add_x_forwarded_for"):
        assert variable in template


def test_the_fragment_is_sourced_by_the_entrypoint() -> None:
    """``.envsh`` (sourced, not executed) and executable — either wrong and it is ignored."""
    assert SCRIPT.name.endswith(".envsh")
    assert SCRIPT.stat().st_mode & 0o111, "the entrypoint ignores a non-executable fragment"
    assert "export NGINX_RESOLVER" in SCRIPT.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert f"{SCRIPT.name} /docker-entrypoint.d/" in dockerfile
