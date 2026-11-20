"""Deploy the API on Render and wait until the new version is live.

usage (from backend/): python -m scripts.render_deploy [COMMIT]

Render isn't connected to GitHub through Render's GitHub app, so pushing doesn't deploy the API (the
website on Vercel does deploy on push). This asks Render to build COMMIT, by default the newest commit
on origin/main, with RENDER_API_KEY from backend/.env, then waits for the result. Push first: Render
builds from GitHub, not from this computer.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

SERVICE_NAME = "chatreceipt-api"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
FINISHED = {"live", "build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed"}


def api_key() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "RENDER_API_KEY" and value.strip():
            return value.strip().strip("\"'")
    raise SystemExit("RENDER_API_KEY isn't set in backend/.env (Render > Account settings > API keys).")


def checked(response: httpx.Response) -> object:
    response.raise_for_status()
    return response.json()


def main() -> None:
    if len(sys.argv) > 1:
        commit = sys.argv[1]
    else:
        commit = subprocess.run(["git", "rev-parse", "origin/main"], capture_output=True, text=True, check=True).stdout.strip()
    with httpx.Client(base_url="https://api.render.com/v1", headers={"Authorization": f"Bearer {api_key()}"}, timeout=30) as render:
        found = checked(render.get("/services", params={"name": SERVICE_NAME, "limit": 1}))
        if not found:
            raise SystemExit(f"No Render service is called {SERVICE_NAME}.")
        service = found[0]["service"]  # type: ignore[index]
        deploy = checked(render.post(f"/services/{service['id']}/deploys", json={"commitId": commit}))
        print(f"Deploying {commit[:7]} to {service['serviceDetails']['url']}")
        status, shown = deploy["status"], ""  # type: ignore[index]
        while status not in FINISHED:
            if status != shown:
                print(f"  {status}")
                shown = status
            time.sleep(15)
            status = checked(render.get(f"/services/{service['id']}/deploys/{deploy['id']}"))["status"]  # type: ignore[index]
    print("Live." if status == "live" else f"Finished as {status}: the deploy's log is in the Render dashboard.")
    raise SystemExit(0 if status == "live" else 1)


if __name__ == "__main__":
    main()
