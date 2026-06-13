# Reference: OAuth 2.0 (`/platform/v1/oauth`)

The OAuth surface lets the assistant access external accounts (Google first) on
the user's behalf. All OAuth machinery — the consent flow, token exchange,
refresh, and vault storage — lives in the core. Modules never touch client
secrets, refresh tokens, or the OAuth flow directly; they call
`GET /platform/v1/oauth/{provider}/token` and receive a valid access token
(ADR-0016).

---

## Secret paths

All paths are tenant-scoped via `scope_secret_path()` (i.e. stored under
`tenants/<tenant_id>/<base>`):

| Path | Contents | Set by |
| --- | --- | --- |
| `oauth/clients/{provider}` | `{client_id, client_secret}` | Operator (once) |
| `oauth/tokens/{provider}` | `{access_token, refresh_token, expires_at, scope, token_type}` | Core (after consent) |

---

## Connect flow

```
Web shell                     Core                    Provider
    |                            |                        |
    |  GET /{provider}/connect   |                        |
    |--------------------------> |                        |
    |  { auth_url }              |                        |
    | <------------------------- |                        |
    |                            |                        |
    | navigate to auth_url       |                        |
    |---------------------------------------------------> |
    |                            |  callback?code&state   |
    |                            | <--------------------- |
    |                            |  exchange + store      |
    |                            |  tokens in OpenBao     |
    | <------------------------------------------302----- |
    | /settings?oauth_connected={provider}                |
```

---

## `GET /platform/v1/oauth/{provider}/connect`

Initiate the OAuth consent flow.  Returns a URL to redirect the browser to.

**Query parameters**

| Param | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tenant_id` | `str` | core default | Which tenant to connect for. |

**Response**

```json
{ "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=…" }
```

**Error responses**

| Status | Condition |
| --- | --- |
| 400 | Client credentials not found in OpenBao, or unsupported provider. |

---

## `GET /platform/v1/oauth/callback`

Provider redirect target — handled server-side.  After token exchange the
browser is redirected:

- **Success:** `/settings?oauth_connected={provider}`
- **Failure:** `/settings?oauth_error=1`

The state parameter (signed HMAC token) carries the tenant and provider through
the round-trip for CSRF protection.  The window is 10 minutes.

---

## `GET /platform/v1/oauth/{provider}/status`

Check whether a provider is connected for this tenant.

**Query parameters**

| Param | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tenant_id` | `str` | core default | Tenant to check. |

**Response**

```json
{
  "provider": "google",
  "connected": true,
  "scope": "openid email profile"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `provider` | `str` | The provider name echoed back. |
| `connected` | `bool` | Whether tokens are stored in the vault. |
| `scope` | `str \| null` | The granted scope string (omitted if not connected). |

---

## `DELETE /platform/v1/oauth/{provider}`

Disconnect — removes the user token secret from the vault.  The operator's
client credentials are unaffected.

**Query parameters**

| Param | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tenant_id` | `str` | core default | Tenant to disconnect. |

**Response**

```json
{ "status": "ok" }
```

Idempotent — returns `ok` even if the provider was not connected.

---

## `GET /platform/v1/oauth/{provider}/token`

*Module-facing.* Return a valid access token, refreshing it transparently when
within 120 seconds of expiry.  Modules call this; they never see the refresh
token or client secret.

**Query parameters**

| Param | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tenant_id` | `str` | core default | Tenant whose token to return. |

**Response**

```json
{
  "access_token": "ya29.a0…",
  "token_type": "Bearer",
  "expires_at": 1750000000.0
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `access_token` | `str` | A valid (possibly just-refreshed) access token. |
| `token_type` | `str` | Always `"Bearer"` for Google. |
| `expires_at` | `float \| null` | Unix timestamp when the token expires (null if unknown). |

**Error responses**

| Status | Condition |
| --- | --- |
| 400 | Provider not connected, or refresh failed (user must reconnect). |

---

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `OAUTH_REDIRECT_BASE_URL` | `http://localhost:8084` | Public base URL of the server — used to build `redirect_uri`. Must match the URI registered with the provider. |
| `OAUTH_STATE_SECRET` | `change-this-before-use` | HMAC key for the state token. Change before first use; rotating invalidates in-flight connect flows. |

---

## Operator setup (Google)

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the APIs you need (Calendar API, Gmail API, etc.).
3. Under **APIs & Services → Credentials**, create an **OAuth 2.0 Client ID**
   (Web application).
4. Add your public callback URL as an **authorized redirect URI**:
   ```
   http://localhost:8084/platform/v1/oauth/callback
   ```
   (Replace `http://localhost:8084` with the value of `OAUTH_REDIRECT_BASE_URL`.)
5. Store the credentials in OpenBao:
   ```bash
   # Requires OPENBAO_TOKEN and a running OpenBao instance
   docker compose exec -e VAULT_TOKEN=$OPENBAO_TOKEN openbao \
     bao kv put secret/tenants/local/oauth/clients/google \
       client_id="<your-client-id>" \
       client_secret="<your-client-secret>"
   ```
6. Set `OAUTH_STATE_SECRET` to a random string in your `.env` (gitignored).
7. Restart the `core-app` service.

---

## Supported providers

| ID | Endpoint | Default scope |
| --- | --- | --- |
| `google` | `accounts.google.com` | `openid email profile` |

Adding a provider requires extending the dispatch methods in
`epicurus_core_app/oauth/service.py` — the vault and route structure are
provider-agnostic.
