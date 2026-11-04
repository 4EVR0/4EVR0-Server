"""Runs inside the disposable app container with its Caddy's public CA certificate."""
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

context = ssl.create_default_context(cadata=sys.stdin.read())
base = "https://beta.localhost"


def request(path, *, method="GET", headers=None, data=None):
    req = urllib.request.Request(base + path, method=method, headers=headers or {}, data=data)
    try:
        return urllib.request.urlopen(req, context=context, timeout=10)
    except urllib.error.HTTPError as error:
        return error


with request("/") as response:
    assert response.status == 200
    assert "WWW-Authenticate" not in response.headers
    assert b"/static/olive.css" in response.read(), "redesigned UI not packaged"
with request("/static/olive.css") as response:
    assert response.status == 200
    assert b".composer-section" in response.read(), "UI stylesheet missing"
with request("/api/v1/sessions/current") as response:
    assert response.status == 404, "anonymous visitor must reach session API without a login prompt"
with request("/api/v1/sessions/browser", method="POST", data=b"x" * 17000) as response:
    assert response.status == 413, "oversized request must be rejected by the proxy"
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
print("PASS: public HTTPS, packaged UI/CSS, request size limit, private routes, secure session, origin, SSE and offline readiness")
