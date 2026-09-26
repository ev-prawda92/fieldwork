"""Company sign-in against a stand-in OpenID Connect provider."""

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from .conftest import H


class FakeIdP:
    """Discovery, JWKS and a token endpoint that enforces client secret + PKCE."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
        self.codes: dict = {}          # code -> (claims, expected code_challenge)
        self.client_secret = "idp-secret"
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    return self._json({"issuer": idp.issuer, "authorization_endpoint": idp.issuer + "/authorize",
                                       "token_endpoint": idp.issuer + "/token", "jwks_uri": idp.issuer + "/jwks"})
                if self.path == "/jwks":
                    return self._json(idp.jwks)
                self._json({"error": "not_found"}, 404)

            def do_POST(self):
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                if form.get("client_secret") != idp.client_secret:
                    return self._json({"error": "invalid_client"}, 401)
                claims, challenge = idp.codes.pop(form.get("code"), (None, None))
                if not claims:
                    return self._json({"error": "invalid_grant"}, 400)
                got = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if got != challenge:
                    return self._json({"error": "invalid_grant", "error_description": "PKCE mismatch"}, 400)
                token = jwt.encode(claims, idp.key, algorithm="RS256", headers={"kid": "k1"})
                self._json({"access_token": "x", "token_type": "Bearer", "id_token": token})

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def claims(self, nonce, email, over):
        now = int(time.time())
        return {"iss": self.issuer, "aud": "fieldwork-client", "sub": "sub-" + email, "email": email,
                "email_verified": True, "name": email.split("@")[0].title(), "nonce": nonce,
                "iat": now, "exp": now + 300, **over}


@pytest.fixture()
def idp(client, monkeypatch):
    monkeypatch.setenv("FIELDWORK_ALLOW_INSECURE_SSO", "1")
    monkeypatch.setenv("FIELDWORK_PUBLIC_URL", "http://testserver")
    p = FakeIdP()
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    cfg["sso"] = {"enabled": True, "issuer": p.issuer, "client_id": "fieldwork-client",
                  "allowed_domains": ["meridian.example"], "jit_role": "", "required": False}
    assert client.put("/api/config", headers=H("head"), json=cfg).status_code == 200
    assert client.put("/api/sso/secret", headers=H("head"), json={"client_secret": p.client_secret}).status_code == 200
    yield p
    p.srv.shutdown()


def sign_in(client, idp, email, **claim_overrides):
    r = client.get("/auth/sso/meridian/start", follow_redirects=False)
    assert r.status_code == 302
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == "http://testserver/auth/sso/callback"
    idp.codes["code-1"] = (idp.claims(q["nonce"], email, claim_overrides), q["code_challenge"])
    cb = client.get(f"/auth/sso/callback?state={q['state']}&code=code-1", follow_redirects=False)
    assert cb.status_code == 302
    loc = cb.headers["location"]
    frag = parse_qs(urlparse(loc).fragment)
    return frag.get("session", [None])[0], frag.get("sso_error", [None])[0], q["state"]


def test_sso_sign_in_for_existing_person(client, idp):
    session, err, _ = sign_in(client, idp, "maya@meridian.example")
    assert err is None and session.startswith("fwsess_")
    me = client.get("/api/me", headers={"Authorization": f"Bearer {session}"}).json()
    assert me["user"]["name"] == "Maya Chen" and me["signed_in_via"] == "sso"
    actions = [e["action"] for e in client.get("/api/audit", headers=H("head")).json()[:1]]
    assert actions == ["auth.sso_login"]
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {session}"})
    assert client.get("/api/me", headers={"Authorization": f"Bearer {session}"}).status_code == 401


def test_unknown_person_needs_jit(client, idp):
    _, err, _ = sign_in(client, idp, "newhire@meridian.example")
    assert "don't have an account" in err
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    cfg["sso"]["jit_role"] = "fde"
    client.put("/api/config", headers=H("head"), json=cfg)
    session, err, _ = sign_in(client, idp, "newhire@meridian.example")
    me = client.get("/api/me", headers={"Authorization": f"Bearer {session}"}).json()
    assert me["user"]["role"] == "fde" and me["user"]["email"] == "newhire@meridian.example"


@pytest.mark.parametrize("override,expect", [
    ({"aud": "someone-else"}, "audience"),
    ({"nonce": "forged"}, "nonce"),
    ({"email": "maya@gmail.com"}, "domain"),
    ({"email_verified": False}, "verified"),
    ({"exp": int(time.time()) - 3600}, "expired"),
])
def test_bad_id_tokens_are_rejected(client, idp, override, expect):
    override = dict(override)
    session, err, _ = sign_in(client, idp, override.pop("email", "maya@meridian.example"), **override)
    assert session is None and expect in err.lower()


def test_state_is_single_use(client, idp):
    _, _, state = sign_in(client, idp, "maya@meridian.example")
    cb = client.get(f"/auth/sso/callback?state={state}&code=code-1", follow_redirects=False)
    assert "already used" in cb.headers["location"] or "expired" in cb.headers["location"]


def test_client_secret_is_encrypted_and_required_mode_blocks_tokens(client, idp):
    row = client.conn.execute("SELECT secret FROM tenant_secrets WHERE name='sso_client_secret'").fetchone()
    assert row["secret"].startswith("enc:v1:") and "idp-secret" not in row["secret"]
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    cfg["sso"]["required"] = True
    assert client.put("/api/config", headers=H("head"), json=cfg).status_code == 200
    assert client.get("/api/me", headers=H("fde")).status_code == 401      # must use company sign-in
    assert client.get("/api/me", headers=H("head")).status_code == 200     # break-glass admin
    session, _, _ = sign_in(client, idp, "maya@meridian.example")
    assert client.get("/api/me", headers={"Authorization": f"Bearer {session}"}).status_code == 200


def test_sso_is_per_workspace(client, idp):
    assert client.get("/api/auth/workspace/meridian").json()["sso"] is True
    assert client.get("/api/auth/workspace/orbital").json()["sso"] is False
    assert client.get("/auth/sso/orbital/start", follow_redirects=False).status_code == 409
