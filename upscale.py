"""Optional artwork upscaling.

When fetched art is smaller than the monitor it will fill, upscale it:
  provider "topaz" -> Topaz Labs Image API (Gigapixel models, cloud, credits)
  provider "ai"    -> the OpenAI-compatible endpoint from the AI section
                      (images API; best-effort, not all servers support it)

Results are cached next to the source as <role>_upscaled.jpg.
"""

import os
import re
import time

import requests
from PIL import Image

TOPAZ_BASE = "https://api.topazlabs.com/image/v1"

# models served by the /enhance-gen/async (creative/generative) endpoint;
# anything else goes to the precision /enhance/async endpoint
TOPAZ_GENERATIVE_MODELS = {
    "Wonder", "Wonder 3", "Redefine", "Bloom", "Bloom 2", "Bloom Realism",
    "Reimagine", "Standard MAX", "Recovery V2",
}


def _model_for(topaz, role):
    """Per-role override (topaz.models.hero/logo/icon) falling back to topaz.model."""
    return (topaz.get("models", {}).get(role)
            or topaz.get("model") or "Standard V2")


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _needs_upscale(path, target_w, target_h):
    with Image.open(path) as im:
        w, h = im.size
    return (w < target_w or h < target_h), (w, h)


def _topaz(src, dst, cfg, log, target_w, target_h, context=None, role=None,
           timeout_s=600):
    topaz = cfg.get("topaz", {})
    key = topaz.get("api_key")
    if not key:
        log("  [up] topaz selected but no API key configured")
        return None
    model = _model_for(topaz, role)
    endpoint_pref = topaz.get("endpoint", "auto")
    generative = (endpoint_pref == "enhance-gen"
                  or (endpoint_pref == "auto" and model in TOPAZ_GENERATIVE_MODELS))
    with Image.open(src) as im:
        sw, sh = im.size
        has_alpha = (im.mode in ("RGBA", "LA")
                     or (im.mode == "P" and "transparency" in im.info))
    out_fmt = "png" if has_alpha else "jpeg"  # keep logo/icon transparency
    # proportional upscale that COVERS the target box (no letterboxing);
    # the compositor center-crops afterward
    scale = max(target_w / sw, target_h / sh, 1.0)
    out_w = min(32000, round(sw * scale))
    out_h = min(32000, round(sh * scale))

    data = {"model": model, "output_format": out_fmt,
            "output_width": out_w, "output_height": out_h}
    if generative:
        data["creativity"] = str(topaz.get("creativity", 3))
        prompt = (topaz.get("prompt") or "").strip()
        if prompt and context:
            prompt = prompt.format(game=context.get("game", ""),
                                   mood=context.get("mood", ""))[:1024]
        if prompt:
            data["prompt"] = prompt
        else:
            data["autoprompt"] = "true"
        timeout_s = max(timeout_s, 900)  # generative jobs run longer
        log(f"  [up] generative model '{model}' (creativity {data['creativity']}, "
            f"prompt: {data.get('prompt', '<autoprompt>')[:80]})")

    headers = {"X-API-Key": key}
    r = requests.post(
        TOPAZ_BASE + ("/enhance-gen/async" if generative else "/enhance/async"),
        headers=headers, timeout=60, data=data,
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
                              timeout=60)
            if dl.status_code == 200 and "json" in dl.headers.get("Content-Type", ""):
                # endpoint returns JSON with a signed R2 URL, not image bytes
                url = dl.json().get("download_url") or dl.json().get("head_url")
                if not url:
                    log(f"  [up] topaz download: no URL in {dl.text[:200]}")
                    return None
                dl = requests.get(url, timeout=300)
            if dl.status_code == 200:
                import io
                try:
                    Image.open(io.BytesIO(dl.content)).verify()
                except Exception:
                    log(f"  [up] topaz download: not an image ({dl.text[:150] if len(dl.content) < 2000 else 'bad bytes'})")
                    return None
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


def maybe_upscale(path, target_w, target_h, cfg, log=print, context=None,
                  role=None):
    """Return an upscaled path if needed+configured, else the original path."""
    up = cfg.get("upscaling", {})
    if not up.get("enabled"):
        return path
    needed, (w, h) = _needs_upscale(path, target_w, target_h)
    if not needed:
        return path
    # cache key includes the effective model, so switching models regenerates
    model = _model_for(cfg.get("topaz", {}), role) if up.get("provider") == "topaz" else "ai"
    dst = os.path.splitext(path)[0] + f"_upscaled_{_slug(model)}.png"
    if os.path.exists(dst):
        return dst  # one upscale per source+model; never re-spend credits
    log(f"  [up] {os.path.basename(path)} is {w}x{h}, target {target_w}x{target_h}, "
        f"model '{model}' — upscaling")
    if up.get("provider") == "topaz":
        out = _topaz(path, dst, cfg, log, target_w, target_h, context, role)
    else:
        out = _ai(path, dst, cfg, log)
    if out:
        with Image.open(out) as im:
            log(f"  [up] done: {im.size[0]}x{im.size[1]} -> {out}")
        return out
    log("  [up] falling back to original (PIL will stretch it instead)")
    return path
