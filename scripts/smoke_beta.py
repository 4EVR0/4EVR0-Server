"""Isolated Compose integration; no published ports, GPU or external data calls."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = "4evr0-beta-smoke-" + uuid.uuid4().hex[:10]
    network_ids = subprocess.check_output(["docker", "network", "ls", "-q"], text=True).split()
    networks = json.loads(subprocess.check_output(["docker", "network", "inspect", *network_ids], text=True)) if network_ids else []
    used = [ipaddress.ip_network(c["Subnet"]) for n in networks for c in (n.get("IPAM", {}).get("Config") or []) if c.get("Subnet")]
    subnet = next(ipaddress.ip_network(f"172.29.{i}.0/28") for i in range(1, 255)
                  if not any(ipaddress.ip_network(f"172.29.{i}.0/28").overlaps(n) for n in used if n.version == 4))
    env = {**os.environ, "APP_IMAGE": args.image, "RUNTIME_ENV_FILE": "/dev/null", "PROXY_ENV_FILE": "/dev/null",
           "PROXY_SUBNET": str(subnet), "CADDY_IP": str(subnet.network_address + 2),
           "APP_IP": str(subnet.network_address + 3)}
    compose = ["docker", "compose", "--project-name", project, "--env-file", "/dev/null",
               "-f", "compose.beta.yml", "-f", "tests/container/compose.smoke.yml"]

    def run(arguments, **kwargs):
        return subprocess.run(arguments, cwd=root, env=env, check=True, timeout=300, **kwargs)

    try:
        run(compose + ["config", "--quiet"])
        run(compose + ["up", "--detach", "--wait", "--wait-timeout", "120"])
        cert = None
        for _ in range(30):
            result = subprocess.run(compose + ["exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt"], cwd=root, env=env, capture_output=True, text=True)
            if result.returncode == 0:
                cert = result.stdout
                break
            time.sleep(1)
        if not cert:
            raise RuntimeError("Test Caddy CA was not created")
        run(compose + ["exec", "-T", "app", "python", "/fixtures/proxy_client.py"], input=cert, text=True)
    except Exception:
        subprocess.run(compose + ["logs", "--tail", "60"], cwd=root, env=env)
        raise
    finally:
        # Only this uniquely named disposable stack and its test volumes.
        run(compose + ["down", "--volumes", "--remove-orphans"])


if __name__ == "__main__":
    main()
