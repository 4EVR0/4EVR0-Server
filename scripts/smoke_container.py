"""Check a built image without GPU/DB access or publishing host ports.

Usage: python3 scripts/smoke_container.py IMAGE --platform linux/amd64
Requires only Python's standard library and a running Docker daemon.
"""

import argparse
import json
import subprocess
import sys
import uuid


CONTAINER_CHECK = r'''
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

assert os.getuid() != 0, "app must run as a non-root user"
root = Path("/app")
for name in (".env", ".env.judge", ".git", "review", "eval", "tests", ".venv", "mlruns"):
    assert not (root / name).exists(), f"unexpected image content: {name}"
assert not list((root / "app").rglob(".env*")), "nested environment file in image"

from app.core.config import settings
from app.prompts import load_prompt

assert settings.app_version == "container-smoke", "runtime environment was not applied"
assert not settings.warmup_enabled, "smoke must not call external dependencies"
assert load_prompt(settings.gen_prompt_name), "generation prompt missing"
assert load_prompt("profile_extraction"), "extraction prompt missing"
for name in ("product_concerns.json", "verified_ingredient_studies.json"):
    assert json.loads((root / "app" / "data" / name).read_text()), f"empty data: {name}"

deadline = time.monotonic() + 60
while True:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=2) as response:
            assert response.status == 200
            assert "text/html" in response.headers["Content-Type"]
            assert "<html" in response.read().decode().lower()
        break
    except (urllib.error.URLError, TimeoutError):
        if time.monotonic() >= deadline:
            raise RuntimeError("web UI did not become available within 60 seconds")
        time.sleep(0.25)

with urllib.request.urlopen("http://127.0.0.1:8000/static/logo.png", timeout=2) as response:
    assert response.read(8) == b"\x89PNG\r\n\x1a\n", "static logo missing"
with urllib.request.urlopen("http://127.0.0.1:8000/openapi.json", timeout=2) as response:
    schema = json.load(response)
    assert schema["info"]["version"] == "container-smoke"
    assert "/api/v1/recommend/stream" in schema["paths"]
with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=2) as response:
    assert response.status == 200
    assert b"http_requests_total" in response.read()
print("PASS: non-root, runtime configuration, assets, prompts, data, UI, API schema, metrics")
'''


def docker(*args, **kwargs):
    return subprocess.run(
        ["docker", *args], text=True, capture_output=True,
        check=True, timeout=30, **kwargs,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--expected-revision")
    args = parser.parse_args()

    metadata = json.loads(docker("image", "inspect", args.image).stdout)[0]
    platform = f"{metadata['Os']}/{metadata['Architecture']}"
    if args.platform and platform != args.platform:
        raise RuntimeError(f"image platform {platform} does not match {args.platform}")
    if args.expected_revision:
        revision = (metadata["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
        if revision != args.expected_revision:
            raise RuntimeError("image revision does not match expected commit")

    # The network namespace has loopback only. Existing DBs/GPU are unreachable.
    container = docker(
        "create", "--pull=never", "--platform", platform,
        "--name", f"4evr0-image-smoke-{uuid.uuid4().hex[:12]}",
        "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--env", "WARMUP_ENABLED=false", "--env", "APP_VERSION=container-smoke",
        args.image,
    ).stdout.strip()
    try:
        docker("start", container)
        result = subprocess.run(
            ["docker", "exec", "-i", container, "python", "-"],
            input=CONTAINER_CHECK, text=True, capture_output=True, check=True, timeout=90,
        )
        print(result.stdout.strip())
        docker("exec", container, "python", "-m", "pip", "check")
        docker("stop", "--time", "10", container)
        state = json.loads(docker("inspect", "--format", "{{json .State}}", container).stdout)
        if state["ExitCode"] != 0 or state["OOMKilled"]:
            raise RuntimeError(f"unclean shutdown: exit={state['ExitCode']}, oom={state['OOMKilled']}")
        print(f"PASS: {platform}, dependency consistency, graceful shutdown")
    except Exception:
        logs = subprocess.run(["docker", "logs", container], text=True, capture_output=True)
        print(logs.stdout + logs.stderr, file=sys.stderr)
        raise
    finally:
        docker("rm", "--force", container)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or "", file=sys.stderr)
        print(exc.stderr or "", file=sys.stderr)
        sys.exit(exc.returncode)
