"""The Helm chart must stay in lockstep with the compose stack (#890).

Two classes of drift this guards against:

* **A module wired into compose but not into the chart.** That is the
  "wire a new module in completely" rule (AGENTS.md) applied to the second
  runtime: `task new-module` writes the chart entry too, and this test fails if a
  hand-rolled module ever skips it.
* **Config that was copied, then diverged.** The chart carries its own copy of
  `nats-server.conf` and SearXNG's `settings.yml` (Helm can only read files
  inside the chart directory), so both are asserted byte-identical to the
  compose originals.

Plus an end-to-end exercise of the OpenBao bootstrap script against a stub of the
OpenBao and Kubernetes APIs — the one piece of the chart that `helm template` and
`kubeconform` cannot say anything about, and the one whose failure mode (an
unseal key that only ever existed in a pod's memory) is unrecoverable.
"""

from __future__ import annotations

import base64
import http.server
import json
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CHART = REPO / "infra" / "k8s" / "epicurus"

# core-app and web are first-class in the chart (their own templates); every other
# compose-included service is a module rendered from `.Values.modules`.
_NON_MODULE_SERVICES = {"core-app", "web"}

_INCLUDE_RE = re.compile(r"^\s*-\s*services/(?P<name>[a-z0-9-]+)/compose\.yaml\s*$", re.MULTILINE)


def _compose_services() -> set[str]:
    """Every service the root compose.yaml includes, from its `include:` list."""
    text = (REPO / "compose.yaml").read_text(encoding="utf-8")
    return {m.group("name") for m in _INCLUDE_RE.finditer(text)}


def _chart_values() -> dict[str, Any]:
    loaded = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_chart_declares_every_compose_service() -> None:
    services = _compose_services()
    # Guard against the include regex silently matching nothing.
    assert {"core-app", "web", "echo", "knowledge"} <= services

    modules = set(_chart_values()["modules"])
    assert modules | _NON_MODULE_SERVICES == services, (
        "the chart's app workloads and the compose stack's services have diverged — "
        "run `task new-module` for a new module, or add the missing "
        "infra/k8s/epicurus/values.yaml `modules` entry by hand"
    )


def test_core_app_and_web_have_their_own_templates() -> None:
    for name in sorted(_NON_MODULE_SERVICES):
        assert (CHART / "templates" / f"{name}.yaml").is_file()


def test_every_module_entry_is_switchable() -> None:
    for name, cfg in _chart_values()["modules"].items():
        assert isinstance(cfg, dict), f"modules.{name} must be a mapping"
        assert "enabled" in cfg, f"modules.{name} needs an explicit `enabled`"


def test_chart_copy_of_nats_config_matches_compose() -> None:
    assert (CHART / "files" / "nats-server.conf").read_bytes() == (
        REPO / "infra" / "compose" / "nats-server.conf"
    ).read_bytes()


def test_chart_copy_of_searxng_settings_matches_compose() -> None:
    assert (CHART / "files" / "searxng-settings.yml").read_bytes() == (
        REPO / "infra" / "searxng" / "settings.yml"
    ).read_bytes()


# ── the OpenBao bootstrap script, against a stub of both APIs ─────────────────

BOOTSTRAP = CHART / "scripts" / "openbao-bootstrap.sh"
NAMESPACE = "epicurus-test"
SECRET_NAME = "epicurus-openbao"
UNSEAL_KEY = "dGhlLXVuc2VhbC1rZXk="
ROOT_TOKEN = "root-token-value"


class _FakeStack:
    """State behind the stub: an OpenBao server and a Kubernetes secret store."""

    def __init__(self) -> None:
        self.initialized = False
        self.sealed = True
        self.kv_mounted = False
        self.policies: dict[str, str] = {}
        self.valid_tokens: set[str] = set()
        self.tokens_created = 0
        self.revoked: list[str] = []
        self.kv: dict[str, Any] = {}
        self.secrets: dict[str, dict[str, str]] = {}
        # Simulates a namespace where the ServiceAccount may read a Secret but not
        # write one (an admission policy, a quota, an operator-supplied account).
        self.deny_secret_writes = False


def _handler(stack: _FakeStack) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:  # keep pytest output readable
            return

        # -- helpers ------------------------------------------------------
        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            parsed = json.loads(self.rfile.read(length))
            assert isinstance(parsed, dict)
            return parsed

        def _send(self, code: int, payload: dict[str, Any] | None = None) -> None:
            raw = json.dumps(payload or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _seal_status(self) -> dict[str, Any]:
            return {"initialized": stack.initialized, "sealed": stack.sealed, "t": 1, "n": 1}

        # -- routing ------------------------------------------------------
        def do_GET(self) -> None:
            if self.path.startswith("/v1/sys/seal-status"):
                self._send(200, self._seal_status())
            elif self.path == "/v1/auth/token/lookup-self":
                token = self.headers.get("X-Vault-Token") or ""
                if token and token in stack.valid_tokens:
                    self._send(200, {"data": {"display_name": "epicurus-core-app"}})
                else:
                    self._send(403, {"errors": ["permission denied"]})
            elif self.path.startswith(f"/api/v1/namespaces/{NAMESPACE}/secrets/"):
                name = self.path.rsplit("/", 1)[-1]
                if name in stack.secrets:
                    self._send(
                        200,
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": name},
                            "data": stack.secrets[name],
                        },
                    )
                else:
                    self._send(404, {"kind": "Status", "code": 404})
            else:
                self._send(404, {})

        def do_POST(self) -> None:
            body = self._body()
            if self.path == "/v1/sys/init":
                if stack.initialized:
                    self._send(400, {"errors": ["already initialized"]})
                    return
                stack.initialized = True
                stack.sealed = True
                # The **HTTP API's** shape, verbatim: `keys` (hex) and
                # `keys_base64`. This stub used to answer with `unseal_keys_b64`,
                # which is what the *CLI* (`bao operator init -format=json`) emits —
                # so the script's matching mistake was invisible here and only
                # surfaced on the first real cluster boot (#894), after it had
                # initialised a vault and discarded its only unseal key. A stub that
                # mirrors the code under test proves nothing; this one mirrors OpenBao.
                self._send(
                    200,
                    {
                        "keys": ["hexkey"],
                        "keys_base64": [UNSEAL_KEY],
                        "root_token": ROOT_TOKEN,
                    },
                )
            elif self.path == "/v1/sys/unseal":
                if body.get("key") != UNSEAL_KEY:
                    self._send(400, {"errors": ["invalid key"]})
                    return
                stack.sealed = False
                stack.valid_tokens.add(ROOT_TOKEN)
                self._send(200, self._seal_status())
            elif self.path == "/v1/sys/mounts/secret":
                if stack.kv_mounted:
                    self._send(400, {"errors": ["path is already in use at secret/"]})
                    return
                stack.kv_mounted = True
                self._send(204)
            elif self.path == "/v1/auth/token/create":
                if self.headers.get("X-Vault-Token") != ROOT_TOKEN:
                    self._send(403, {"errors": ["permission denied"]})
                    return
                stack.tokens_created += 1
                token = f"app-token-{stack.tokens_created}"
                stack.valid_tokens.add(token)
                self._send(
                    200,
                    {
                        "auth": {
                            "client_token": token,
                            "policies": ["epicurus-core"],
                            "period": 2764800,
                        }
                    },
                )
            elif self.path == "/v1/auth/token/revoke":
                stack.revoked.append(str(body.get("token")))
                stack.valid_tokens.discard(str(body.get("token")))
                self._send(204)
            elif self.path.startswith("/v1/secret/data/"):
                stack.kv[self.path] = body.get("data")
                self._send(200, {"data": {"version": 1}})
            elif self.path == f"/api/v1/namespaces/{NAMESPACE}/secrets":
                if stack.deny_secret_writes:
                    self._send(403, {"kind": "Status", "code": 403})
                    return
                name = body["metadata"]["name"]
                if name in stack.secrets:
                    self._send(409, {"kind": "Status", "code": 409})
                    return
                stack.secrets[name] = dict(body["data"])
                self._send(201, body)
            else:
                self._send(404, {})

        def do_PUT(self) -> None:
            body = self._body()
            if self.path.startswith("/v1/sys/policies/acl/"):
                stack.policies[self.path.rsplit("/", 1)[-1]] = str(body.get("policy"))
                self._send(204)
            else:
                self._send(404, {})

        def do_PATCH(self) -> None:
            body = self._body()
            if self.path.startswith(f"/api/v1/namespaces/{NAMESPACE}/secrets/"):
                if stack.deny_secret_writes:
                    self._send(403, {"kind": "Status", "code": 403})
                    return
                name = self.path.rsplit("/", 1)[-1]
                if name not in stack.secrets:
                    self._send(404, {"kind": "Status", "code": 404})
                    return
                stack.secrets[name].update(body["data"])
                self._send(200, {"metadata": {"name": name}, "data": stack.secrets[name]})
            else:
                self._send(404, {})

    return Handler


@pytest.fixture
def fake_stack(tmp_path: Path) -> Iterator[tuple[_FakeStack, str, Path]]:
    """A stubbed OpenBao + Kubernetes API, and a fake ServiceAccount token dir."""
    stack = _FakeStack()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(stack))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    sa_dir = tmp_path / "serviceaccount"
    sa_dir.mkdir()
    (sa_dir / "token").write_text("fake-sa-token", encoding="utf-8")
    (sa_dir / "ca.crt").write_text("", encoding="utf-8")

    try:
        yield stack, base, sa_dir
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_bootstrap(
    base: str, sa_dir: Path, *, store_nats: str = "false"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(BOOTSTRAP)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "BAO_ADDR": base,
            "K8S_API": base,
            "SA_DIR": str(sa_dir),
            "SECRET_NAME": SECRET_NAME,
            "NAMESPACE": NAMESPACE,
            "TENANT": "local",
            "STORE_NATS": store_nats,
            "NATS_CORE_PASSWORD": "core-pw",
            "NATS_MODULE_PASSWORD": "module-pw",
            "NATS_SYS_PASSWORD": "sys-pw",
        },
    )


def _decoded(stack: _FakeStack, key: str) -> str:
    return base64.b64decode(stack.secrets[SECRET_NAME][key]).decode()


pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None, reason="the bootstrap script needs curl"
)


def test_bootstrap_initialises_unseals_and_stores_the_token(
    fake_stack: tuple[_FakeStack, str, Path],
) -> None:
    stack, base, sa_dir = fake_stack

    result = _run_bootstrap(base, sa_dir)

    assert result.returncode == 0, result.stderr
    assert stack.initialized and not stack.sealed
    assert stack.kv_mounted
    # The policy must grant the two self-management paths: the app token is minted
    # with no default policy, and core-app renews its own lease daily (#728).
    policy = stack.policies["epicurus-core"]
    assert "secret/data/tenants/*" in policy
    assert "auth/token/renew-self" in policy
    assert _decoded(stack, "unseal-key") == UNSEAL_KEY
    assert _decoded(stack, "root-token") == ROOT_TOKEN
    assert _decoded(stack, "app-token") == "app-token-1"


def test_bootstrap_is_idempotent(fake_stack: tuple[_FakeStack, str, Path]) -> None:
    stack, base, sa_dir = fake_stack

    assert _run_bootstrap(base, sa_dir).returncode == 0
    second = _run_bootstrap(base, sa_dir)

    assert second.returncode == 0, second.stderr
    # No second vault, no second token: the hook runs on every release.
    assert stack.tokens_created == 1
    assert _decoded(stack, "app-token") == "app-token-1"


def test_bootstrap_reunseals_a_restarted_vault(
    fake_stack: tuple[_FakeStack, str, Path],
) -> None:
    stack, base, sa_dir = fake_stack
    assert _run_bootstrap(base, sa_dir).returncode == 0

    stack.sealed = True  # the pod restarted; a file-backed vault comes back sealed

    assert _run_bootstrap(base, sa_dir).returncode == 0
    assert not stack.sealed
    assert stack.tokens_created == 1


def test_bootstrap_mints_a_new_token_when_the_old_one_stopped_working(
    fake_stack: tuple[_FakeStack, str, Path],
) -> None:
    stack, base, sa_dir = fake_stack
    assert _run_bootstrap(base, sa_dir).returncode == 0

    stack.valid_tokens.discard("app-token-1")  # revoked / expired

    assert _run_bootstrap(base, sa_dir).returncode == 0
    assert stack.tokens_created == 2
    assert _decoded(stack, "app-token") == "app-token-2"
    # The superseded token is revoked, not left orphaned-and-periodic forever.
    assert stack.revoked == ["app-token-1"]


def test_bootstrap_refuses_to_guess_when_the_unseal_key_is_gone(
    fake_stack: tuple[_FakeStack, str, Path],
) -> None:
    stack, base, sa_dir = fake_stack
    stack.initialized = True  # a surviving PVC...
    stack.secrets.clear()  # ...and someone deleted the Secret

    result = _run_bootstrap(base, sa_dir)

    assert result.returncode != 0
    assert "holds no unseal key" in result.stderr


def test_bootstrap_records_the_nats_passwords_in_openbao(
    fake_stack: tuple[_FakeStack, str, Path],
) -> None:
    stack, base, sa_dir = fake_stack

    assert _run_bootstrap(base, sa_dir, store_nats="true").returncode == 0

    assert stack.kv["/v1/secret/data/tenants/local/nats"] == {
        "core": "core-pw",
        "module": "module-pw",
        "sys": "sys-pw",
    }


def test_bootstrap_refuses_to_initialise_when_it_cannot_store_the_key(
    fake_stack: tuple[_FakeStack, str, Path],
) -> None:
    """The unrecoverable failure: a vault created whose only unseal key is lost.

    If the Secret write is going to fail, it must fail *before* `/sys/init`, not
    after — afterwards the key exists only in this pod's memory and the data on the
    volume is gone for good.
    """
    stack, base, sa_dir = fake_stack
    stack.deny_secret_writes = True

    result = _run_bootstrap(base, sa_dir)

    assert result.returncode != 0
    assert not stack.initialized, "initialised a vault it could not store the key for"
    assert "bootstrap-probe" in result.stderr
