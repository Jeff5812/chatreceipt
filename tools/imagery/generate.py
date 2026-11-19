"""Generate an image from a text prompt with a free, keyless service.

usage: python generate.py <service> <prompt.txt> <out-file> [--width W] [--height H] [--seed N] [--model M]
services:
  pollinations  image.pollinations.ai (no key)
  horde         AI Horde, anonymous key (no account; slow queue)
  hfspace       the public FLUX.1-schnell demo on Hugging Face (anonymous quota)
Only the prompt text is sent. No keys or personal details leave this machine.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

UA = {"User-Agent": "chatreceipt-imagery/1.0"}


def pollinations(args, prompt: str) -> bytes:
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt, safe="")
    params = {"width": args.width, "height": args.height, "model": args.model or "flux", "nologo": "true", "seed": args.seed}
    with httpx.Client(timeout=httpx.Timeout(240, connect=30), follow_redirects=True, headers=UA) as client:
        response = client.get(url, params=params)
    print("pollinations:", response.status_code, response.headers.get("content-type"), len(response.content), "bytes")
    if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
        sys.exit("pollinations refused: " + response.text[:400])
    return response.content


HORDE_PRESETS = {
    # model name on the Horde: sampler settings that suit it
    "Z-Image-Turbo": {"steps": 9, "cfg_scale": 1, "sampler_name": "k_euler"},
    "Flux.1-Schnell fp8 (Compact)": {"steps": 4, "cfg_scale": 1, "sampler_name": "k_euler"},
    "AlbedoBase XL (SDXL)": {"steps": 28, "cfg_scale": 6, "sampler_name": "k_euler_a"},
    "Juggernaut XL": {"steps": 28, "cfg_scale": 5, "sampler_name": "k_dpmpp_2m", "karras": True},
}


def _retry(call, tries: int = 6):
    last = None
    for attempt in range(tries):
        try:
            return call()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:  # the Horde drops connections now and then
            last = exc
            time.sleep(4 + attempt * 3)
    raise last


def horde(args, prompt: str) -> bytes:
    headers = {**UA, "apikey": "0000000000", "Client-Agent": "chatreceipt-imagery:1.0:anonymous"}
    model = args.model or "Z-Image-Turbo"
    params = {"width": args.width, "height": args.height, "n": 1, "seed": str(args.seed), **HORDE_PRESETS.get(model, HORDE_PRESETS["AlbedoBase XL (SDXL)"])}
    body = {"prompt": prompt, "params": params, "models": [model], "nsfw": False, "censor_nsfw": True, "r2": True, "shared": False, "slow_workers": True}
    with httpx.Client(timeout=httpx.Timeout(60, connect=30), headers=headers, follow_redirects=True) as client:
        started = _retry(lambda: client.post("https://aihorde.net/api/v2/generate/async", json=body))
        if started.status_code >= 300:
            sys.exit(f"horde refused: {started.status_code} {started.text[:500]}")
        job = started.json()["id"]
        print("horde job", job, "kudos", started.json().get("kudos"), "model", model, flush=True)
        deadline = time.time() + args.wait
        last_line = ""
        while time.time() < deadline:
            time.sleep(10)
            check = _retry(lambda: client.get(f"https://aihorde.net/api/v2/generate/check/{job}")).json()
            line = f"  queue {check.get('queue_position')} wait {check.get('wait_time')}s processing {check.get('processing')} possible {check.get('is_possible')}"
            if line != last_line:
                print(line, flush=True)
                last_line = line
            if check.get("faulted"):
                sys.exit("horde job faulted")
            if check.get("done"):
                break
        else:
            try:
                client.delete(f"https://aihorde.net/api/v2/generate/status/{job}")
            except httpx.HTTPError:
                pass
            sys.exit("horde: gave up waiting")
        status = _retry(lambda: client.get(f"https://aihorde.net/api/v2/generate/status/{job}")).json()
        generation = status["generations"][0]
        print("  made by model", generation.get("model"), "censored", generation.get("censored"))
        image = generation["img"]
        if image.startswith("http"):
            return _retry(lambda: client.get(image)).content
        return base64.b64decode(image)


def hfspace(args, prompt: str) -> bytes:
    """The public FLUX.1-schnell demo. Uses the queue API so a refusal (such as the daily allowance running out) is reported in words."""
    import uuid

    base = "https://black-forest-labs-flux-1-schnell.hf.space"
    with httpx.Client(timeout=httpx.Timeout(240, connect=30), headers=UA, follow_redirects=True) as client:
        config = client.get(base + "/config").json()
        prefix = config.get("api_prefix", "")
        fn = next(d["id"] for d in config["dependencies"] if d.get("api_name") == "infer")
        session = uuid.uuid4().hex[:12]
        joined = client.post(f"{base}{prefix}/queue/join", json={"data": [prompt, args.seed, False, args.width, args.height, 4], "fn_index": fn, "session_hash": session})
        if joined.status_code != 200:
            sys.exit(f"hf space refused: {joined.status_code} {joined.text[:300]}")
        with client.stream("GET", f"{base}{prefix}/queue/data", params={"session_hash": session}) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                message = json.loads(line[6:])
                if message.get("msg") != "process_completed":
                    continue
                output = message.get("output") or {}
                if not message.get("success"):
                    sys.exit(f"hf space: {message.get('title') or 'error'}: {str(output.get('error'))[:300]}")
                file_info = output["data"][0]
                url = file_info.get("url") or f"{base}{prefix}/file={file_info['path']}"
                return client.get(url).content
    sys.exit("hf space: the stream ended without an image")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["pollinations", "horde", "hfspace"])
    parser.add_argument("prompt")
    parser.add_argument("out")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--model", default=None)
    parser.add_argument("--wait", type=int, default=420)
    args = parser.parse_args()
    prompt = Path(args.prompt).read_text(encoding="utf-8").strip()
    started = time.time()
    data = {"pollinations": pollinations, "horde": horde, "hfspace": hfspace}[args.service](args, prompt)
    Path(args.out).write_bytes(data)
    print(f"saved {args.out}: {len(data)} bytes in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
