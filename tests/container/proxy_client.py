"""Runs inside the disposable app container with its Caddy's public CA certificate."""
import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

context = ssl.create_default_context(cadata=sys.stdin.read())
base = "https://beta.localhost"
authorization = "Basic " + base64.b64encode(b"smoke:hiccup").decode()


def request(path, *, auth=True, method="GET", headers=None):
    combined = {"Authorization": authorization} if auth else {}
    combined.update(headers or {})
    req = urllib.request.Request(base + path, method=method, headers=combined)
    try:
        return urllib.request.urlopen(req, context=context, timeout=10)
    except urllib.error.HTTPError as error:
        return error


for path in ("/", "/api/v1/sessions/current"):
    with request(path, auth=False) as response:
        assert response.status == 401, (path, response.status)
with request("/") as response:
    assert response.status == 200
for path in ("/health", "/live", "/ready", "/metrics", "/docs", "/openapi.json"):
    with request(path) as response:
        assert response.status == 404, (path, response.status)
with request("/api/v1/sessions/browser", method="POST", headers={"Origin": base, "X-Forwarded-Proto": "http"}) as response:
    assert response.status == 201, (response.status, response.read())
    cookie = response.headers["Set-Cookie"]
    assert all(flag in cookie for flag in ("Secure", "HttpOnly", "SameSite=strict")), cookie
with request("/api/v1/sessions/current", headers={"Cookie": cookie.split(";", 1)[0]}) as response:
    assert response.status == 200
    assert json.load(response) == {"turns": []}
with request("/api/v1/sessions/browser", method="POST", headers={"Origin": "https://untrusted.example"}) as response:
    assert response.status == 403
with request("/__smoke/stream") as response:
    assert response.readline() == b"data: first\n"
    first = time.monotonic()
    assert response.readline() == b"\n"
    assert response.readline() == b"data: second\n"
    assert time.monotonic() - first > 0.5, "SSE was buffered until completion"
with urllib.request.urlopen("http://127.0.0.1:8000/live", timeout=3) as response:
    assert response.status == 200
try:
    urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=10)
    raise AssertionError("Unavailable GPU/graph must fail readiness")
except urllib.error.HTTPError as response:
    assert response.status == 503
    deps = json.load(response)["dependencies"]
    assert deps == {"postgresql": "ok", "redis": "ok", "neo4j": "error", "llm": "error"}, deps
print("PASS: verified HTTPS, invite auth, private routes, secure session, origin, SSE and offline readiness")
