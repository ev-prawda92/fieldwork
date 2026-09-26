"""Company sign-in: OpenID Connect against the customer's identity provider.

Works with any OIDC provider (Okta, Microsoft Entra ID / Azure AD, Google
Workspace, Auth0, Ping, Keycloak). Flow: authorization code + PKCE.

    /auth/sso/<workspace-slug>/start  ->  IdP login  ->  /auth/sso/callback
        -> verify the ID token (signature via the IdP's JWKS, issuer, audience,
           expiry, nonce, verified email, allowed domain)
        -> match the person by email, or create them with the workspace's
           just-in-time role if that's switched on
        -> issue a session (12h) and hand it to the console in the URL fragment

Per-workspace settings live in config["sso"]; the client secret is stored
encrypted in tenant_secrets, never in config.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt

from . import plugins

STATE_TTL_S = 600
SESSION_HOURS = 12
_cache: dict[str, tuple[float, dict]] = {}


class SSOError(ValueError):
    pass


def allow_insecure() -> bool:
    """http issuers and private addresses, for local testing only."""
    return os.environ.get("FIELDWORK_ALLOW_INSECURE_SSO") == "1"


def public_url() -> str:
    return os.environ.get("FIELDWORK_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")


def redirect_uri() -> str:
    return public_url() + "/auth/sso/callback"


def _get_json(url: str, data: dict | None = None) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and not allow_insecure():
        raise SSOError("identity provider URLs must be https")
    try:
        plugins._guard_host(url, allow_private=allow_insecure())
    except plugins.PluginError as e:
        raise SSOError(str(e))
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json"})
    try:
        with plugins._opener.open(req, timeout=15) as r:
            return json.loads(r.read(1_000_000))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read(10_000)).get("error_description", "")
        except Exception:
            pass
        raise SSOError(f"identity provider returned HTTP {e.code} {detail}".strip())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SSOError(f"couldn't reach identity provider: {getattr(e, 'reason', e)}")


def discovery(issuer: str) -> dict:
    hit = _cache.get("d:" + issuer)
    if hit and hit[0] > time.time():
        return hit[1]
    doc = _get_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if k not in doc:
            raise SSOError(f"identity provider discovery is missing {k}")
    _cache["d:" + issuer] = (time.time() + 3600, doc)
    return doc


def _jwks(uri: str, force: bool = False) -> dict:
    hit = _cache.get("j:" + uri)
    if hit and hit[0] > time.time() and not force:
        return hit[1]
    doc = _get_json(uri)
    _cache["j:" + uri] = (time.time() + 3600, doc)
    return doc


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(sso: dict, state: str, nonce: str, challenge: str) -> str:
    d = discovery(sso["issuer"])
    q = {"response_type": "code", "client_id": sso["client_id"], "redirect_uri": redirect_uri(),
         "scope": "openid email profile", "state": state, "nonce": nonce,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    return d["authorization_endpoint"] + ("&" if "?" in d["authorization_endpoint"] else "?") + \
        urllib.parse.urlencode(q)


def exchange(sso: dict, client_secret: str, code: str, verifier: str) -> str:
    d = discovery(sso["issuer"])
    tok = _get_json(d["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": sso["client_id"], "client_secret": client_secret, "code_verifier": verifier})
    if "id_token" not in tok:
        raise SSOError("identity provider didn't return an ID token")
    return tok["id_token"]


def verify_id_token(sso: dict, id_token: str, nonce: str) -> dict:
    d = discovery(sso["issuer"])
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise SSOError("malformed ID token")
    if header.get("alg") not in ("RS256", "ES256", "PS256"):
        raise SSOError(f"ID token algorithm {header.get('alg')!r} isn't accepted")
    key = None
    for force in (False, True):  # refetch once if the IdP rotated keys
        for k in _jwks(d["jwks_uri"], force).get("keys", []):
            if k.get("kid") == header.get("kid"):
                key = jwt.PyJWK(k)
                break
        if key:
            break
    if not key:
        raise SSOError("ID token signed with an unknown key")
    try:
        claims = jwt.decode(id_token, key=key, algorithms=[header["alg"]], audience=sso["client_id"],
                            issuer=d["issuer"], options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                            leeway=60)
    except jwt.PyJWTError as e:
        raise SSOError(f"ID token rejected: {e}")
    if claims.get("nonce") != nonce:
        raise SSOError("ID token nonce doesn't match this sign-in")
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise SSOError("identity provider didn't share an email address")
    if claims.get("email_verified") is False:
        raise SSOError("email address isn't verified at the identity provider")
    domains = [x.lower().lstrip("@") for x in sso.get("allowed_domains", [])]
    if domains and email.rsplit("@", 1)[-1] not in domains:
        raise SSOError("that email domain isn't allowed for this workspace")
    return {**claims, "email": email}
