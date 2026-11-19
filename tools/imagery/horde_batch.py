"""Submit several image jobs to AI Horde at once (anonymous, free) and save each as it finishes.

usage: python horde_batch.py <manifest.json> [--wait 1500]
manifest: [{"prompt": "path.txt", "out": "path.webp", "width": 960, "height": 1088, "seed": 11, "model": "Z-Image-Turbo"}, ...]
Anonymous jobs must be at most 1024 x 1024 pixels in total. Only the prompt text is sent.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from generate import HORDE_PRESETS, UA  # noqa: E402

API = "https://aihorde.net/api/v2"


def call(client: httpx.Client, method: str, url: str, **kwargs):
    last = None
    for attempt in range(6):
        try:
            return client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last = exc
            time.sleep(3 + attempt * 3)
    raise last


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--wait", type=int, default=1500)
    args = parser.parse_args()
    jobs = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    headers = {**UA, "apikey": "0000000000", "Client-Agent": "chatreceipt-imagery:1.0:anonymous"}
    started = time.time()
    with httpx.Client(timeout=httpx.Timeout(60, connect=30), headers=headers, follow_redirects=True) as client:
        pending = {}
        for job in jobs:
            if Path(job["out"]).exists():
                print("skip (exists)", job["out"], flush=True)
                continue
            model = job.get("model", "Z-Image-Turbo")
            params = {"width": job["width"], "height": job["height"], "n": 1, "seed": str(job.get("seed", 7)), **HORDE_PRESETS[model]}
            body = {
                "prompt": Path(job["prompt"]).read_text(encoding="utf-8").strip(),
                "params": params,
                "models": [model],
                "nsfw": False,
                "censor_nsfw": True,
                "r2": True,
                "shared": False,
                "slow_workers": True,
            }
            response = call(client, "POST", f"{API}/generate/async", json=body)
            if response.status_code >= 300:
                print("refused", job["out"], response.status_code, response.text[:260], flush=True)
                continue
            pending[response.json()["id"]] = job
            print("queued", Path(job["out"]).name, model, flush=True)
            time.sleep(1.5)

        deadline = time.time() + args.wait
        last_report = 0.0
        while pending and time.time() < deadline:
            time.sleep(12)
            for job_id in list(pending):
                try:
                    check = call(client, "GET", f"{API}/generate/check/{job_id}").json()
                except Exception as exc:  # keep the batch alive
                    print("check failed", type(exc).__name__, flush=True)
                    continue
                job = pending[job_id]
                if check.get("faulted") or check.get("is_possible") is False:
                    print("cannot run", Path(job["out"]).name, {k: check.get(k) for k in ("faulted", "is_possible")}, flush=True)
                    pending.pop(job_id)
                    continue
                if check.get("done"):
                    status = call(client, "GET", f"{API}/generate/status/{job_id}").json()
                    generations = status.get("generations") or []
                    if not generations:
                        print("no image", Path(job["out"]).name, flush=True)
                        pending.pop(job_id)
                        continue
                    if generations[0].get("censored"):
                        # The service's safety filter sometimes blocks harmless prompts and returns a notice image.
                        print("blocked by the service's filter, not saved:", Path(job["out"]).name, flush=True)
                        pending.pop(job_id)
                        continue
                    image = generations[0]["img"]
                    data = call(client, "GET", image).content if image.startswith("http") else base64.b64decode(image)
                    Path(job["out"]).write_bytes(data)
                    print(f"saved {Path(job['out']).name} {len(data)} bytes after {time.time() - started:.0f}s censored={generations[0].get('censored')}", flush=True)
                    pending.pop(job_id)
                elif time.time() - last_report > 60:
                    print(f"  waiting {Path(job['out']).name}: queue {check.get('queue_position')} eta {check.get('wait_time')}s processing {check.get('processing')}", flush=True)
            if time.time() - last_report > 60:
                last_report = time.time()
        for job_id, job in pending.items():
            print("gave up on", Path(job["out"]).name, flush=True)
            try:
                client.delete(f"{API}/generate/status/{job_id}")
            except httpx.HTTPError:
                pass
    print("batch finished", flush=True)


if __name__ == "__main__":
    main()
