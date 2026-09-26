"""Sign-in is available on every platform epicurus ships for (#969).

The core is the OpenID Connect relying party; this file holds the two *deployments* to the same
contract, the way ``test_no_local_runtime.py`` does for the hosted-only mode:

* **Compose** — parsed from the fragment directly: every key the core reads is passed through
  from ``.env`` with the core's own default, and the switches and the day count never arrive
  blank, because the core parses a blank boolean or integer as an error, not as "unset".
* **Chart** — real ``helm template`` renders, skipped (never silently passed) without ``helm``:

  - a release that says nothing about sign-in renders **exactly one** entry more than a chart
    without the sign-in wiring — ``AUTH_MODE=none`` — and ``none`` ignores every other ``auth``
    value, so switching sign-in off really switches all of it off;
  - ``oidc`` renders every key, with the client id from a value, from
    ``auth.oidc.existingSecret`` or from the shared Secret, and the client secret only ever
    from a Secret, optional (a public client has none);
  - the render-time guard refuses what the core would refuse to start with, naming the fix;
  - no container ever carries a duplicate env name — server-side apply (Flux) rejects one;
  - NOTES prints the callback to register, or warns when a published release has sign-in off.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CHART = REPO / "infra" / "k8s" / "epicurus"
CORE_FRAGMENT = REPO / "services" / "core-app" / "compose.yaml"
ENV_EXAMPLE = REPO / ".env.example"

# The core's sign-in configuration (#969), and the default the Compose fragment hands it when
# `.env` says nothing. `None` = no default: passed through blank, which the core reads as unset.
_CONTRACT: dict[str, str | None] = {
    "AUTH_MODE": "none",
    "AUTH_SESSION_DAYS": "30",
    "OIDC_ISSUER_URL": None,
    "OIDC_PROVIDER_NAME": None,
    "OIDC_CLIENT_ID": None,
    "OIDC_CLIENT_SECRET": None,
    "OIDC_SCOPES": "openid email profile",
    "OIDC_ALLOWED_EMAILS": None,
    "OIDC_ALLOWED_GROUPS": None,
    "OIDC_ALLOW_ALL_USERS": "false",
    "OIDC_AUTO_REDIRECT": "false",
}
_SIGN_IN_ENV = frozenset(_CONTRACT)


# ── Compose ──────────────────────────────────────────────────────────────────────


def _compose_core_env() -> dict[str, str]:
    loaded = yaml.safe_load(CORE_FRAGMENT.read_text(encoding="utf-8"))
    env = loaded["services"]["core-app"]["environment"]
    assert isinstance(env, dict)
    return {str(k): str(v) for k, v in env.items()}


@pytest.mark.parametrize("key", sorted(_CONTRACT))
def test_compose_passes_every_sign_in_key_through(key: str) -> None:
    """Each key reaches the core from `.env`, defaulting to exactly what the core defaults to.

    ``:-``, not ``-`` — the opposite of ``OLLAMA_URL``, and on purpose. There a blank value is
    a statement ("no local runtime"); here it is an operator who wrote ``AUTH_MODE=`` or
    ``OIDC_ALLOW_ALL_USERS=`` in `.env` and meant the default. For the switches and the day
    count that is not a nicety: the core parses a blank boolean or integer as a start-up error.
    """
    assert _compose_core_env()[key] == f"${{{key}:-{_CONTRACT[key] or ''}}}"


def test_env_example_documents_every_sign_in_key() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in _CONTRACT:
        assert re.search(rf"^# {key}=", text, re.MULTILINE), f".env.example never mentions {key}"
    assert "docs/infrastructure/sign-in.md" in text, ".env.example should point at the guide"
    # The example is a placeholder — a filled-in client secret would be a leaked credential.
    assert re.search(r"^# OIDC_CLIENT_SECRET=\s", text, re.MULTILINE)


# ── Chart ────────────────────────────────────────────────────────────────────────

pytestmark_helm = pytest.mark.skipif(
    shutil.which("helm") is None, reason="these assertions render the chart with helm"
)

# A sign-in the core accepts: an issuer, a client id and an admission rule.
_OIDC = (
    "auth.mode=oidc",
    "auth.oidc.issuerUrl=https://id.example.com",
    "auth.oidc.clientId=epicurus-client",
    "auth.oidc.allowedEmails={you@example.com}",
)

# Explicit passwords, so two renders of the chart-generated Secret are byte-comparable
# (`randAlphaNum` otherwise differs on every render).
_PINNED_SECRETS = (
    "secrets.postgresPassword=pg",
    "secrets.natsCorePassword=core",
    "secrets.natsModulePassword=module",
    "secrets.natsSysPassword=sys",
    "secrets.oauthStateSecret=state",
    "secrets.minioRootPassword=minio",
)


def _sets(*pairs: str) -> list[str]:
    args: list[str] = []
    for pair in pairs:
        args += ["--set", pair]
    return args


def _render(*args: str, chart: Path = CHART) -> subprocess.CompletedProcess[str]:
    """`helm template` with raw arguments (`--set`, `--set-string`, `--show-only`, …)."""
    return subprocess.run(
        ["helm", "template", "epicurus", str(chart), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _rendered(*args: str, chart: Path = CHART) -> str:
    result = _render(*args, chart=chart)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _docs(rendered: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]


def _containers(rendered: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every container and init container of every pod template in the render."""
    for doc in _docs(rendered):
        template = (doc.get("spec") or {}).get("template")
        if not isinstance(template, dict):
            continue
        pod = template["spec"]
        for container in [*pod.get("initContainers", []), *pod.get("containers", [])]:
            yield f"{doc['kind']}/{doc['metadata']['name']}/{container['name']}", container


def _core_env(rendered: str) -> dict[str, dict[str, Any]]:
    """The core-app container's env, keyed by name (values *and* valueFrom kept)."""
    for where, container in _containers(rendered):
        if where == "Deployment/core-app/core-app":
            return {e["name"]: e for e in container["env"]}
    raise AssertionError("no core-app Deployment in the render")


def _without_sign_in(tmp_path: Path) -> Path:
    """A copy of the chart with the sign-in wiring cut out — the chart as it rendered before #969.

    Whole lines, leading indentation and newline included, so the copy renders byte-for-byte
    what the template produced before the two lines were added. Each must be found exactly
    once: a refactor that moves them fails here, loudly, instead of quietly comparing a chart
    with itself.
    """
    copy = tmp_path / "without-sign-in" / "epicurus"
    shutil.copytree(CHART, copy)
    template = copy / "templates" / "core-app.yaml"
    text = template.read_text(encoding="utf-8")
    for wiring in (
        '{{- include "epicurus.assertSignIn" . -}}\n',
        '            {{- include "epicurus.signInEnv" $ | nindent 12 }}\n',
    ):
        assert text.count(wiring) == 1, f"core-app.yaml no longer carries {wiring!r} verbatim"
        text = text.replace(wiring, "")
    template.write_text(text, encoding="utf-8")
    return copy


@pytestmark_helm
@pytest.mark.parametrize(
    "shape",
    [
        (),
        ("ingress.enabled=true", "networkPolicy.enabled=true", "metrics.podMonitor.enabled=true"),
        (
            "ollama.enabled=false",
            "core.llm.defaultModel=claude/claude-sonnet-4-6",
            "core.memoryEmbedModel=gpt/text-embedding-3-small",
        ),
    ],
    ids=["default", "published", "hosted-only"],
)
def test_a_release_that_says_nothing_about_sign_in_gains_exactly_one_env_entry(
    tmp_path: Path, shape: tuple[str, ...]
) -> None:
    """The promise that makes sign-in safe to ship off: nothing else about a release moves.

    Rendered against the same chart with the two sign-in lines removed, so the comparison is
    the whole release — every resource, every label, every line — and it stays meaningful as
    the chart changes around it.
    """
    args = _sets(*_PINNED_SECRETS, *shape)
    before = _rendered(*args, chart=_without_sign_in(tmp_path)).splitlines()
    after = _rendered(*args).splitlines()

    delta = [
        line
        for line in difflib.unified_diff(before, after, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert delta == ["+            - name: AUTH_MODE", '+              value: "none"'], delta


@pytestmark_helm
def test_none_mode_ignores_every_other_sign_in_value() -> None:
    """Switching sign-in off switches all of it off — not "off, but still wired to a Secret"."""
    pinned = _sets(*_PINNED_SECRETS)
    default = _rendered(*pinned)
    configured_but_off = _rendered(
        *pinned,
        *_sets(
            "auth.mode=none",
            "auth.sessionDays=7",
            "auth.oidc.issuerUrl=https://id.example.com",
            "auth.oidc.providerName=Pocket ID",
            "auth.oidc.clientId=epicurus-client",
            "auth.oidc.existingSecret=epicurus-oidc",
            "auth.oidc.scopes=openid email profile groups",
            "auth.oidc.allowedEmails={you@example.com}",
            "auth.oidc.allowedGroups={family}",
            "auth.oidc.allowAllUsers=true",
            "auth.oidc.autoRedirect=true",
        ),
    )
    assert configured_but_off == default

    env = _core_env(default)
    assert {name for name in env if name in _SIGN_IN_ENV} == {"AUTH_MODE"}
    assert env["AUTH_MODE"] == {"name": "AUTH_MODE", "value": "none"}


@pytestmark_helm
def test_oidc_renders_every_key_the_core_reads() -> None:
    env = _core_env(_rendered(*_sets(*_OIDC)))

    assert {name for name in env if name in _SIGN_IN_ENV} == _SIGN_IN_ENV
    assert env["AUTH_MODE"]["value"] == "oidc"
    assert env["OIDC_ISSUER_URL"]["value"] == "https://id.example.com"
    assert env["OIDC_CLIENT_ID"] == {"name": "OIDC_CLIENT_ID", "value": "epicurus-client"}
    assert env["OIDC_ALLOWED_EMAILS"]["value"] == "you@example.com"
    # The core's own defaults, spelled out rather than left for it to fill in.
    assert env["AUTH_SESSION_DAYS"]["value"] == "30"
    assert env["OIDC_SCOPES"]["value"] == "openid email profile"
    assert env["OIDC_ALLOWED_GROUPS"]["value"] == ""
    assert env["OIDC_PROVIDER_NAME"]["value"] == ""
    assert env["OIDC_ALLOW_ALL_USERS"]["value"] == "false"
    assert env["OIDC_AUTO_REDIRECT"]["value"] == "false"


def _secret_ref(entry: dict[str, Any]) -> dict[str, Any]:
    assert "value" not in entry, f"{entry['name']} is in the clear, not from a Secret"
    ref = entry["valueFrom"]["secretKeyRef"]
    assert isinstance(ref, dict)
    return ref


@pytestmark_helm
def test_the_client_secret_only_ever_comes_from_a_secret_and_may_be_absent() -> None:
    """A public client (PKCE only) has no secret, so the key is optional — never a crash loop."""
    env = _core_env(_rendered(*_sets(*_OIDC)))
    assert _secret_ref(env["OIDC_CLIENT_SECRET"]) == {
        "name": "epicurus-secrets",
        "key": "OIDC_CLIENT_SECRET",
        "optional": True,
    }


@pytestmark_helm
def test_the_client_id_can_come_from_the_operators_secret() -> None:
    env = _core_env(
        _rendered(
            *_sets(
                "auth.mode=oidc",
                "auth.oidc.issuerUrl=https://id.example.com",
                "auth.oidc.existingSecret=epicurus-oidc",
                "auth.oidc.allowAllUsers=true",
            )
        )
    )
    # Required, unlike the secret: a sign-in with no client id cannot work, so a Secret missing
    # the key should stop the pod with CreateContainerConfigError, not start a broken core.
    assert _secret_ref(env["OIDC_CLIENT_ID"]) == {"name": "epicurus-oidc", "key": "OIDC_CLIENT_ID"}
    assert _secret_ref(env["OIDC_CLIENT_SECRET"]) == {
        "name": "epicurus-oidc",
        "key": "OIDC_CLIENT_SECRET",
        "optional": True,
    }


@pytestmark_helm
def test_without_its_own_secret_the_client_falls_back_to_the_shared_one() -> None:
    env = _core_env(
        _rendered(
            *_sets(
                "secrets.existingSecret=my-shared",
                "auth.mode=oidc",
                "auth.oidc.issuerUrl=https://id.example.com",
                "auth.oidc.allowedGroups={family}",
            )
        )
    )
    assert _secret_ref(env["OIDC_CLIENT_ID"]) == {"name": "my-shared", "key": "OIDC_CLIENT_ID"}
    assert _secret_ref(env["OIDC_CLIENT_SECRET"])["name"] == "my-shared"


@pytestmark_helm
def test_a_client_id_value_wins_over_the_secret() -> None:
    env = _core_env(_rendered(*_sets(*_OIDC, "auth.oidc.existingSecret=epicurus-oidc")))
    assert env["OIDC_CLIENT_ID"] == {"name": "OIDC_CLIENT_ID", "value": "epicurus-client"}
    assert _secret_ref(env["OIDC_CLIENT_SECRET"])["name"] == "epicurus-oidc"


@pytestmark_helm
def test_lists_are_comma_joined_and_switches_render_as_the_core_reads_them() -> None:
    env = _core_env(
        _rendered(
            *_sets(
                *_OIDC,
                "auth.oidc.allowedEmails={ You@Example.com , partner@example.com }",
                "auth.sessionDays=7",
                "auth.oidc.scopes=openid email profile groups",
                "auth.oidc.allowAllUsers=true",
                "auth.oidc.autoRedirect=true",
            ),
            # One comma-free string is what `--set key=value` hands a template for a list key.
            "--set",
            "auth.oidc.allowedGroups=family",
        )
    )
    assert env["OIDC_ALLOWED_EMAILS"]["value"] == "You@Example.com,partner@example.com"
    assert env["OIDC_ALLOWED_GROUPS"]["value"] == "family"
    assert env["AUTH_SESSION_DAYS"]["value"] == "7"
    assert env["OIDC_SCOPES"]["value"] == "openid email profile groups"
    assert env["OIDC_ALLOW_ALL_USERS"]["value"] == "true"
    assert env["OIDC_AUTO_REDIRECT"]["value"] == "true"


# ── the render-time guard ─────────────────────────────────────────────────────────


def _refused(*args: str) -> str:
    result = _render(*args)
    assert result.returncode != 0, "the chart rendered a sign-in the core would refuse"
    return result.stderr


@pytestmark_helm
@pytest.mark.parametrize("mode", ["OIDC", "", "basic"])
def test_an_unknown_mode_is_refused(mode: str) -> None:
    err = _refused(*_sets(f"auth.mode={mode}"))
    assert "auth.mode" in err
    assert '"none"' in err and '"oidc"' in err, "the refusal must name both valid modes"


@pytestmark_helm
def test_oidc_without_an_issuer_is_refused() -> None:
    err = _refused(
        *_sets(
            "auth.mode=oidc", "auth.oidc.clientId=epicurus-client", "auth.oidc.allowAllUsers=true"
        )
    )
    assert "auth.oidc.issuerUrl" in err


@pytestmark_helm
@pytest.mark.parametrize(
    "issuer",
    ["id.example.com", "ftp://id.example.com", "https://", "https://id.example.com/?x=1"],
)
def test_oidc_with_an_issuer_the_core_cannot_use_is_refused(issuer: str) -> None:
    # The core's `_url_problem`: an absolute http(s) URL with a host, no query or fragment.
    err = _refused(
        *_sets(
            "auth.mode=oidc",
            f"auth.oidc.issuerUrl={issuer}",
            "auth.oidc.clientId=epicurus-client",
            "auth.oidc.allowAllUsers=true",
        )
    )
    assert "auth.oidc.issuerUrl" in err and "absolute http(s) URL" in err


@pytestmark_helm
def test_oidc_with_a_public_url_the_core_cannot_use_is_refused() -> None:
    err = _refused(*_sets(*_OIDC, "core.oauth.redirectBaseUrl=assistant.example.com"))
    assert "core.oauth.redirectBaseUrl" in err and "absolute http(s) URL" in err


@pytestmark_helm
def test_an_issuer_with_a_path_and_the_smoke_gates_issuer_render() -> None:
    for issuer in ("https://id.example.com/realms/home", "http://127.0.0.1:9/smoke-issuer"):
        env = _core_env(
            _rendered(
                *_sets(
                    "auth.mode=oidc",
                    f"auth.oidc.issuerUrl={issuer}",
                    "auth.oidc.clientId=epicurus-client",
                    "auth.oidc.allowAllUsers=true",
                )
            )
        )
        assert env["OIDC_ISSUER_URL"]["value"] == issuer


@pytestmark_helm
def test_oidc_without_a_client_id_source_is_refused() -> None:
    """The chart-generated Secret never holds OIDC keys, so it cannot be the fallback on its own."""
    err = _refused(
        *_sets(
            "auth.mode=oidc",
            "auth.oidc.issuerUrl=https://id.example.com",
            "auth.oidc.allowAllUsers=true",
        )
    )
    assert "auth.oidc.clientId" in err
    assert "auth.oidc.existingSecret" in err
    assert "OIDC_CLIENT_ID" in err


@pytestmark_helm
@pytest.mark.parametrize(
    "admission",
    [
        (),
        ("--set", "auth.oidc.allowedEmails={,}"),
        # `\,` is how `--set` carries a literal comma: the value is " , ".
        ("--set", "auth.oidc.allowedGroups= \\, "),
        ("--set-string", "auth.oidc.allowAllUsers=false"),
    ],
    ids=["nothing", "a-list-of-blanks", "a-string-of-blanks", "the-string-false"],
)
def test_oidc_without_an_admission_rule_is_refused(admission: tuple[str, ...]) -> None:
    """An empty allowlist is never "everyone" — against Google that would admit the internet.

    Including the ways an allowlist can *look* set: a list of blanks, and `--set-string`'s
    "false", which a template reads as truthy.
    """
    err = _refused(
        *_sets(
            "auth.mode=oidc",
            "auth.oidc.issuerUrl=https://id.example.com",
            "auth.oidc.clientId=epicurus-client",
        ),
        *admission,
    )
    for key in ("auth.oidc.allowedEmails", "auth.oidc.allowedGroups", "auth.oidc.allowAllUsers"):
        assert key in err, f"the refusal must offer {key}"
    assert "everyone" in err and "Google" in err, "the refusal must say why"


@pytestmark_helm
@pytest.mark.parametrize("days", ["0", "-3", "soon"])
def test_a_session_of_no_days_is_refused(days: str) -> None:
    err = _refused(*_sets(*_OIDC, f"auth.sessionDays={days}"))
    assert "auth.sessionDays" in err


def _family_in_helpers() -> list[str]:
    """The names `epicurus.signInEnvNames` declares, read from the template source."""
    text = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    match = re.search(
        r'define "epicurus\.signInEnvNames" -}}\n(?P<names>[A-Z_,]+)\n', text, re.MULTILINE
    )
    assert match, "epicurus.signInEnvNames is gone from _helpers.tpl"
    return match.group("names").split(",")


def test_the_guarded_family_is_exactly_the_core_contract() -> None:
    """The guard and the env template read one list; that list must be the whole contract."""
    names = _family_in_helpers()
    assert len(names) == len(set(names))
    assert set(names) == _SIGN_IN_ENV


@pytestmark_helm
@pytest.mark.parametrize(
    ("name", "mode"),
    [("AUTH_MODE", ()), ("OIDC_CLIENT_SECRET", _OIDC), ("AUTH_SESSION_DAYS", _OIDC)],
)
def test_extra_env_may_not_shadow_a_sign_in_variable(name: str, mode: tuple[str, ...]) -> None:
    """`core.extraEnv` comes last, so a sign-in name there would be a duplicate env entry.

    Server-side apply (Flux) rejects that outright; client-side apply keeps the last one, which
    is worse — the pod runs a sign-in the guard above never saw. One source: `auth.*`.
    """
    err = _refused(*_sets(*mode, f"core.extraEnv.{name}=x"))
    assert f"core.extraEnv sets {name}" in err
    assert "auth." in err


@pytestmark_helm
@pytest.mark.parametrize(
    "shape",
    [
        (),
        _OIDC,
        (
            "auth.mode=oidc",
            "auth.oidc.issuerUrl=https://id.example.com",
            "auth.oidc.existingSecret=epicurus-oidc",
            "auth.oidc.allowAllUsers=true",
            "ingress.enabled=true",
            "networkPolicy.enabled=true",
        ),
        (*_OIDC, "core.extraEnv.MAINTENANCE_SCHEDULE_ENABLED=true"),
    ],
    ids=["default", "oidc", "oidc-secret-published", "oidc-extra-env"],
)
def test_no_container_carries_a_duplicate_env_name(shape: tuple[str, ...]) -> None:
    """Kubernetes keys a container's env by name; server-side apply rejects a repeat."""
    for where, container in _containers(_rendered(*_sets(*shape))):
        names = [entry["name"] for entry in container.get("env", [])]
        repeated = sorted({n for n in names if names.count(n) > 1})
        assert not repeated, f"{where} carries {repeated} more than once"


# ── NOTES ────────────────────────────────────────────────────────────────────────


def _notes(tmp_path: Path, *args: str) -> str:
    """NOTES.txt, rendered by the real template engine with no cluster.

    `helm template` never renders NOTES, and `helm install --dry-run` needs a reachable API
    server. So: copy the chart, turn NOTES.txt into a named template, and render it into a
    ConfigMap — the same engine and values, with every guard still running first.
    """
    copy = tmp_path / "notes" / "epicurus"
    shutil.copytree(CHART, copy)
    templates = copy / "templates"
    notes = (templates / "NOTES.txt").read_text(encoding="utf-8")
    (templates / "NOTES.txt").unlink()
    (templates / "_notes_under_test.tpl").write_text(
        '{{- define "notes-under-test" -}}\n' + notes + "{{- end -}}\n", encoding="utf-8"
    )
    (templates / "notes-under-test.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: notes-under-test\n"
        'data:\n  notes: {{ include "notes-under-test" . | toJson }}\n',
        encoding="utf-8",
    )
    rendered = _rendered(*args, "--show-only", "templates/notes-under-test.yaml", chart=copy)
    text = yaml.safe_load(rendered)["data"]["notes"]
    assert isinstance(text, str)
    return text


_OFF_WARNING = "sign-in is off"


@pytestmark_helm
def test_notes_print_the_callback_to_register(tmp_path: Path) -> None:
    notes = _notes(
        tmp_path,
        *_sets(*_OIDC, "ingress.enabled=true", "ingress.tls.enabled=true"),
        "--set",
        "auth.oidc.providerName=Pocket ID",
    )
    assert "https://epicurus.example.com/platform/v1/auth/callback" in notes
    assert "Pocket ID" in notes
    assert "plain http" not in notes
    assert _OFF_WARNING not in notes


@pytestmark_helm
def test_notes_follow_the_redirect_base_url_and_drop_its_trailing_slash(tmp_path: Path) -> None:
    notes = _notes(tmp_path, *_sets(*_OIDC, "core.oauth.redirectBaseUrl=https://ai.example.com/"))
    assert "https://ai.example.com/platform/v1/auth/callback" in notes
    assert "//platform" not in notes


@pytestmark_helm
def test_notes_flag_a_plain_http_callback(tmp_path: Path) -> None:
    """No ingress → the port-forward address; fine on localhost, but the cookie is not Secure."""
    notes = _notes(tmp_path, *_sets(*_OIDC))
    assert "http://localhost:8084/platform/v1/auth/callback" in notes
    assert "plain http" in notes


@pytestmark_helm
def test_notes_warn_when_a_published_release_has_sign_in_off(tmp_path: Path) -> None:
    notes = _notes(tmp_path, *_sets("ingress.enabled=true"))
    assert _OFF_WARNING in notes
    assert "docs/infrastructure/sign-in.md" in notes
    assert "/platform/v1/auth/callback" not in notes


@pytestmark_helm
def test_notes_stay_quiet_about_sign_in_for_a_private_release(tmp_path: Path) -> None:
    """The default — no ingress, reached by port-forward — is not a published host."""
    notes = _notes(tmp_path)
    assert _OFF_WARNING not in notes
    assert "/platform/v1/auth/callback" not in notes


# ── one closed set of failure codes, three places ────────────────────────────────

_CORE_ERRORS = REPO / "services" / "core-app" / "src" / "epicurus_core_app" / "auth" / "errors.py"
_WEB_AUTH = REPO / "services" / "web" / "src" / "lib" / "auth.ts"
_GUIDE = REPO / "docs" / "infrastructure" / "sign-in.md"


def test_every_auth_error_code_is_spoken_by_the_shell_and_explained_by_the_guide() -> None:
    """The core's `AuthErrorCode`, the shell's sentences and the guide's table are one set.

    The core redirects with the code, the web shell renders a sentence for it and the operator
    guide's troubleshooting table says what to do. A code added in one place only reaches a user
    as a generic sentence — or reaches an operator with no row to look up — so drift fails here.
    """
    core_src = _CORE_ERRORS.read_text(encoding="utf-8")
    literal = re.search(r"AuthErrorCode = Literal\[(.*?)\n\]", core_src, re.DOTALL)
    assert literal, "AuthErrorCode is no longer a Literal in auth/errors.py"
    core = re.findall(r'^\s*"([a-z_]+)",', literal.group(1), re.MULTILINE)

    web_src = _WEB_AUTH.read_text(encoding="utf-8")
    listed = re.search(r"export const AUTH_ERROR_CODES = \[(.*?)\] as const;", web_src, re.DOTALL)
    assert listed, "AUTH_ERROR_CODES is no longer a const array in services/web/src/lib/auth.ts"
    web = re.findall(r'"([a-z_]+)"', listed.group(1))

    rows = re.findall(r"^\| `([a-z_]+)` \|", _GUIDE.read_text(encoding="utf-8"), re.MULTILINE)
    guide = [code for code in rows if code != "auth_error"]  # the table's header cell

    assert len(core) == 10, core
    assert web == core, "the web shell's AUTH_ERROR_CODES drifted from the core's AuthErrorCode"
    assert sorted(guide) == sorted(core), "the guide's troubleshooting table drifted from the core"
