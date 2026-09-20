"""Optional artwork upscaling.

When fetched art is smaller than the monitor it will fill, upscale it:
  provider "topaz" -> Topaz Labs Image API (Gigapixel models, cloud, credits)
  provider "ai"    -> the OpenAI-compatible endpoint from the AI section
                      (images API; best-effort, not all servers support it)

Results are cached next to the source as <role>_upscaled.jpg.
"""

import os
import time

import requests
from PIL import Image

TOPAZ_BASE = "https://api.topazlabs.com/image/v1"


def _needs_upscale(path, target_w, target_h):
    with Image.open(path) as im:
        w, h = im.size
    return (w < target_w or h < target_h), (w, h)


def _topaz(src, dst, cfg, log, timeout_s=600):
    topaz = cfg.get("topaz", {})
    key = topaz.get("api_key")
    if not key:
        log("  [up] topaz selected but no API key configured")
        return None
    headers = {"X-API-Key": key}
    r = requests.post(
        TOPAZ_BASE + "/enhance/async", headers=headers, timeout=60,
        data={"model": topaz.get("model", "Standard V2"), "output_format": "jpeg"},
        files={"image": open(src, "rb")})
    if r.status_code not in (200, 201, 202):
        log(f"  [up] topaz submit failed: HTTP {r.status_code} {r.text[:200]}")
        return None
    pid = r.json().get("process_id")
    log(f"  [up] topaz job {pid} submitted")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        st = requests.get(f"{TOPAZ_BASE}/status/{pid}", headers=headers, timeout=30)
        status = st.json().get("status", "")
        log(f"  [up] topaz status: {status}")
        if status == "Completed":
            dl = requests.get(f"{TOPAZ_BASE}/download/{pid}", headers=headers,
                              timeout=300, allow_redirects=True)
            if dl.status_code == 200 and len(dl.content) > 10000:
                with open(dst, "wb") as f:
                    f.write(dl.content)
                return dst
            log(f"  [up] topaz download failed: HTTP {dl.status_code}")
            return None
        if status in ("Failed", "Cancelled"):
            log(f"  [up] topaz job {status.lower()}")
            return None
    log("  [up] topaz timed out")
    return None


def _ai(src, dst, cfg, log, timeout_s=300):
    """Best-effort OpenAI Images API compatible upscale (/images/edits).
    Few local servers implement this; failures fall back gracefully."""
    ai = cfg.get("ai", {})
    base = (ai.get("base_url") or "").rstrip("/")
    if not base:
        log("  [up] ai provider selected but AI section has no base_url")
        return None
    headers = {"Authorization": f"Bearer {ai.get('api_key') or 'none'}"}
    model = ai.get("upscale_model") or ai.get("model") or ""
    try:
        r = requests.post(
            base + "/images/edits", headers=headers, timeout=timeout_s,
            data={"model": model, "response_format": "b64_json",
                  "prompt": "Upscale this game key art to higher resolution, "
                            "faithful, no content changes."},
            files={"image": open(src, "rb")})
        if r.status_code != 200:
            log(f"  [up] ai images API failed: HTTP {r.status_code} {r.text[:200]}")
            return None
        import base64
        b64 = r.json()["data"][0]["b64_json"]
        with open(dst, "wb") as f:
            f.write(base64.b64decode(b64))
        return dst
    except Exception as e:
        log(f"  [up] ai upscale error: {e}")
        return None


def maybe_upscale(path, target_w, target_h, cfg, log=print):
    """Return an upscaled path if needed+configured, else the original path."""
    up = cfg.get("upscaling", {})
    if not up.get("enabled"):
        return path
    needed, (w, h) = _needs_upscale(path, target_w, target_h)
    if not needed:
        return path
    dst = os.path.splitext(path)[0] + "_upscaled.jpg"
    if os.path.exists(dst):
        with Image.open(dst) as im:
            if im.size[0] >= target_w or im.size[1] >= target_h:
                return dst
    log(f"  [up] {os.path.basename(path)} is {w}x{h}, target {target_w}x{target_h} — upscaling")
    fn = _topaz if up.get("provider") == "topaz" else _ai
    out = fn(path, dst, cfg, log)
    if out:
        with Image.open(out) as im:
            log(f"  [up] done: {im.size[0]}x{im.size[1]} -> {out}")
        return out
    log("  [up] falling back to original (PIL will stretch it instead)")
    return path
