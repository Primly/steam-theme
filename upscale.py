"""Optional artwork upscaling.

When fetched art is smaller than the monitor it will fill, upscale it:
  provider "topaz" -> Topaz Labs Image API (Gigapixel models, cloud, credits)
  provider "ai"    -> the OpenAI-compatible endpoint from the AI section
                      (images API; best-effort, not all servers support it)
  provider "comfy" -> a LOCAL ComfyUI instance: upload the image, submit an
                      Export-API workflow, download the result. Free (your
                      GPU); jobs can be deferred while a game is running.

Results are cached next to the source as <role>_upscaled_<model>.png.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid

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


def fill_prompt(template, context):
    """Substitute {placeholders} from context into a Topaz prompt template.

    Available keys: {game} plus the VLM's JSON values ({mood}, {theme_name},
    {appearance}, {palette_mode}) — and any extra keys the user's custom
    naming prompt asked the VLM for (forwarded by main._upscale_context).
    Unknown placeholders are dropped (never raise on a hand-typed template)
    and reported, so a typo can't fail the pipeline.
    Returns (filled_text, dropped_keys).
    """
    dropped = []

    def sub(m):
        key = m.group(1)
        if context.get(key) is not None:
            return str(context[key])
        dropped.append(key)
        return ""

    out = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template)
    return out, dropped


def _needs_upscale(path, target_w, target_h):
    with Image.open(path) as im:
        w, h = im.size
    return (w < target_w or h < target_h), (w, h)


def cache_path(src, cfg, role=None):
    """Where the upscale of src with the currently configured provider/model
    would be cached. The model slug in the filename means switching models
    (or editing the Comfy workflow) re-generates; old caches go unused."""
    prov = cfg.get("upscaling", {}).get("provider")
    if prov == "comfy":
        model = _comfy_slug(cfg, role)
    elif prov == "topaz":
        model = _model_for(cfg.get("topaz", {}), role)
    else:
        model = "ai"
    return os.path.splitext(src)[0] + f"_upscaled_{_slug(model)}.png"


def pending(src, target_w, target_h, cfg, role=None):
    """True if an upscale pass would actually submit a job for src: it needs
    enlargement toward the target AND has no cached result yet. Never spends
    anything — used for Ultimate Fetch's pre-flight credit estimate."""
    if not cfg.get("upscaling", {}).get("enabled"):
        return False
    needed, _ = _needs_upscale(src, target_w, target_h)
    return needed and not os.path.exists(cache_path(src, cfg, role))


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
        if prompt:
            ctx = dict(context or {})
            if role:
                ctx.setdefault("role", role)
            prompt, dropped = fill_prompt(prompt, ctx)
            if dropped:
                log(f"  [up] unknown prompt placeholders ignored: "
                    f"{', '.join(sorted(set(dropped)))}")
            prompt = prompt[:1024]
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


# ---------------------------------------------------------------- ComfyUI

# Built-in default workflow (Ultimate SD Upscale, SDXL + 4x-UltraSharp).
# Single source of truth: the config UI fetches this via
# GET /api/comfy-default-workflow and shows it as an editable guide — an
# untouched/empty field stays out of the user's config entirely.
DEFAULT_COMFY_WORKFLOW = r"""{
  "1": {
    "inputs": {
      "image": "hero_1.jpg"
    },
    "class_type": "LoadImage",
    "_meta": {
      "title": "Load Image"
    }
  },
  "2": {
    "inputs": {
      "ckpt_name": "sd_xl_base_1.0.safetensors"
    },
    "class_type": "CheckpointLoaderSimple",
    "_meta": {
      "title": "Load Checkpoint"
    }
  },
  "3": {
    "inputs": {
      "vae_name": "sdxl_vae.safetensors"
    },
    "class_type": "VAELoader",
    "_meta": {
      "title": "Load VAE"
    }
  },
  "4": {
    "inputs": {
      "model_name": "4xUltrasharp_v10.pt"
    },
    "class_type": "UpscaleModelLoader",
    "_meta": {
      "title": "Load Upscale Model"
    }
  },
  "5": {
    "inputs": {
      "text": "high-quality official video game key art, preserve original composition,\npreserve character identity, coherent cinematic lighting, detailed environment,\nclean edges, sharp but natural texture, no changes to logos or typography",
      "clip": [
        "2",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "Positive Prompt"
    }
  },
  "6": {
    "inputs": {
      "text": "duplicated subject, extra limbs, deformed face, altered composition, cropped head, blurry, over sharpened, halo, tiling artifacts, jpeg artifacts",
      "clip": [
        "2",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "Negative Prompt"
    }
  },
  "7": {
    "inputs": {
      "image": [
        "1",
        0
      ]
    },
    "class_type": "GetImageSize",
    "_meta": {
      "title": "Get Image Size"
    }
  },
  "8": {
    "inputs": {
      "expression": "2 + 2 * (max(a, b) < 2560)",
      "values.a": [
        "7",
        0
      ],
      "values.b": [
        "7",
        1
      ]
    },
    "class_type": "ComfyMathExpression",
    "_meta": {
      "title": "Math Expression"
    }
  },
  "9": {
    "inputs": {
      "upscale_by": [
        "8",
        0
      ],
      "seed": 20,
      "steps": 25,
      "cfg": 6,
      "sampler_name": "dpmpp_2m",
      "scheduler": "karras",
      "denoise": 0.3,
      "mode_type": "Chess",
      "tile_width": 768,
      "tile_height": 768,
      "mask_blur": 8,
      "tile_padding": 64,
      "seam_fix_mode": "None",
      "seam_fix_denoise": 0.22,
      "seam_fix_width": 16,
      "seam_fix_mask_blur": 8,
      "seam_fix_padding": 16,
      "force_uniform_tiles": 1,
      "tiled_decode": false,
      "batch_size": 1,
      "image": [
        "1",
        0
      ],
      "model": [
        "2",
        0
      ],
      "positive": [
        "5",
        0
      ],
      "negative": [
        "6",
        0
      ],
      "vae": [
        "3",
        0
      ],
      "upscale_model": [
        "4",
        0
      ]
    },
    "class_type": "UltimateSDUpscale",
    "_meta": {
      "title": "Ultimate SD Upscale"
    }
  },
  "11": {
    "inputs": {
      "compare_view": {
        "beforeImages": [
          "/api/view?filename=comfy.compare.a_temp_xqxvb_00001_.png&subfolder=&type=temp&rand=0.9727672303626591"
        ],
        "afterImages": [
          "/api/view?filename=comfy.compare.b_temp_xqxvb_00001_.png&subfolder=&type=temp&rand=0.9727672303626591"
        ]
      },
      "image_a": [
        "1",
        0
      ],
      "image_b": [
        "13",
        0
      ]
    },
    "class_type": "ImageCompare",
    "_meta": {
      "title": "Compare Images"
    }
  },
  "12": {
    "inputs": {
      "expression": "5120 if a >= 5120 else 2560 if a >= 2560 else 1920",
      "values.a": [
        "14",
        0
      ]
    },
    "class_type": "ComfyMathExpression",
    "_meta": {
      "title": "Math Expression"
    }
  },
  "13": {
    "inputs": {
      "resize_type": "scale dimensions",
      "resize_type.width": [
        "12",
        1
      ],
      "resize_type.height": 0,
      "resize_type.crop": "center",
      "scale_method": "lanczos",
      "input": [
        "9",
        0
      ]
    },
    "class_type": "ResizeImageMaskNode",
    "_meta": {
      "title": "Resize Image/Mask"
    }
  },
  "14": {
    "inputs": {
      "image": [
        "9",
        0
      ]
    },
    "class_type": "GetImageSize",
    "_meta": {
      "title": "Get Image Size"
    }
  },
  "15": {
    "inputs": {
      "filename_prefix": "ComfyUI_Upscaled",
      "images": [
        "13",
        0
      ]
    },
    "class_type": "SaveImage",
    "_meta": {
      "title": "Save Image"
    }
  }
}"""

_COMFY_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_queue_lock = threading.Lock()


def _comfy_base(cfg):
    """http://host:port from the comfy config, with validation."""
    c = cfg.get("comfy", {})
    host = str(c.get("host") or "127.0.0.1").strip()
    if not _COMFY_HOST_RE.fullmatch(host):
        raise ValueError(f"bad comfy host {host!r}")
    try:
        port = int(c.get("port") or 8188)
    except (TypeError, ValueError):
        port = 8188
    if not 1 <= port <= 65535:
        raise ValueError(f"bad comfy port {port!r}")
    return f"http://{host}:{port}"


def validate_workflow(text):
    """Validate an Export-API workflow string for upscaling duty. Empty text
    means the built-in default. Returns (parsed_dict_or_None, error_or_None).
    """
    try:
        wf = json.loads((text or "").strip() or DEFAULT_COMFY_WORKFLOW)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(wf, dict) or not wf:
        return None, "workflow must be a JSON object of nodes"
    types = {n.get("class_type") for n in wf.values() if isinstance(n, dict)}
    missing = []
    if "LoadImage" not in types:
        missing.append("a LoadImage node (the image to upscale)")
    if not types & {"SaveImage", "Save Image"}:
        missing.append("a SaveImage node (the upscaled result)")
    if missing:
        return None, "missing " + " and ".join(missing)
    return wf, None


def _workflow_text(c, role=None):
    """The workflow JSON text for an image role: the per-role override when
    set (comfy.workflows.hero/logo/icon), otherwise the shared workflow,
    otherwise empty (= built-in default)."""
    if role:
        w = (c.get("workflows", {}).get(role) or "").strip()
        if w:
            return w
    return (c.get("workflow") or "").strip()


def _workflow(cfg, role=None):
    """The effective workflow (parsed) for a role: per-role override ->
    shared workflow -> built-in default."""
    wf, _ = validate_workflow(_workflow_text(cfg.get("comfy", {}), role))
    return wf


def _comfy_slug(cfg, role=None):
    """Cache slug for the comfy provider: a content hash of the role's
    effective workflow, so editing a workflow re-generates its upscales
    (old caches go unused). Roles sharing a workflow share a slug."""
    wf = _workflow(cfg, role)
    blob = (json.dumps(wf, sort_keys=True) if wf is not None
            else _workflow_text(cfg.get("comfy", {}), role))
    return "comfy-" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def fill_workflow(workflow, context):
    """Return a deep copy of the workflow with {placeholders} filled in every
    string value (prompt text, prefixes — anywhere). Substitution happens on
    the parsed structure, so values containing quotes/newlines can't break
    the JSON. Unknown placeholders are dropped (and reported), same as the
    Topaz prompt template. Returns (workflow, dropped_keys)."""
    dropped = []

    def walk(v):
        if isinstance(v, str):
            out, d = fill_prompt(v, context or {})
            dropped.extend(d)
            return out
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(workflow), dropped


def _game_running(cfg):
    """True when a game is running right now (Steam now-playing or a watched
    custom process). A detection failure must never stall upscales."""
    try:
        import steamdetect
        return bool(steamdetect.get_running_appid()
                    or steamdetect.find_running_custom_game(
                        cfg.get("custom_games")))
    except Exception:
        return False


def _queue_file(cfg):
    return os.path.join(cfg.get("cache_dir") or "cache", "comfy_queue.json")


def enqueue_comfy(cfg, job):
    """Queue a deferred Comfy upscale (deduped by src+dst). Atomic write."""
    path = _queue_file(cfg)
    with _queue_lock:
        try:
            with open(path, encoding="utf-8") as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            q = []
        if any(j.get("src") == job["src"] and j.get("dst") == job["dst"]
               for j in q if isinstance(j, dict)):
            return False
        job.setdefault("attempts", 0)
        q.append(job)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, path)
    return True


def drain_comfy_queue(cfg, log=print):
    """Process queued Comfy upscales once the GPU is free (no game running).
    Returns the cache keys (game folders) that received fresh upscales so the
    service can re-apply a theme that's currently on screen. Never raises."""
    if (cfg.get("upscaling", {}).get("provider") != "comfy"
            or not cfg.get("upscaling", {}).get("enabled")):
        return []
    with _queue_lock:
        try:
            with open(_queue_file(cfg), encoding="utf-8") as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
    if not isinstance(q, list) or not q:
        return []
    if cfg.get("comfy", {}).get("defer_while_gaming", True) \
            and _game_running(cfg):
        return []
    try:
        r = requests.get(_comfy_base(cfg) + "/system_stats", timeout=5)
        reachable = r.status_code == 200
    except Exception:
        reachable = False
    if not reachable:
        log(f"  [up] comfy queue: {len(q)} job(s) pending but ComfyUI is "
            "unreachable")
        return []
    log(f"  [up] comfy queue: processing {len(q)} deferred upscale(s)")
    done_keys, remaining = [], []
    for job in q:
        # the CURRENT config decides the cache slot: if the workflow was
        # edited since enqueue, the job lands under the new slug
        role = job.get("role")
        dst = cache_path(str(job["src"]), cfg, role)
        if os.path.exists(dst):
            continue  # already upscaled since enqueue; just drop the job
        try:
            out = _comfy(str(job["src"]), dst, cfg, log,
                         job.get("w") or 0, job.get("h") or 0,
                         context=job.get("context"), defer=False, role=role)
        except Exception as e:
            log(f"  [up] comfy queue job error: {e}")
            out = None
        if out:
            done_keys.append(str(job.get("cache_key") or ""))
        else:
            job["attempts"] = int(job.get("attempts") or 0) + 1
            if job["attempts"] >= 3:
                log(f"  [up] comfy queue: dropping "
                    f"{os.path.basename(str(job.get('src')))} "
                    "after 3 failed attempts")
            else:
                remaining.append(job)
    with _queue_lock:
        tmp = _queue_file(cfg) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(remaining, f, indent=2)
        os.replace(tmp, _queue_file(cfg))
    return [k for k in done_keys if k]


def _comfy(src, dst, cfg, log, target_w, target_h, context=None,
           defer=True, role=None):
    """Upscale via a local ComfyUI instance: upload the image, submit the
    role's Export-API workflow (placeholders filled), poll history, download
    the result. With defer_while_gaming on and a game running, the job is
    queued instead and drained when the game exits. Never raises."""
    c = cfg.get("comfy", {})
    if defer and c.get("defer_while_gaming", True) and _game_running(cfg):
        job = {"src": src, "dst": dst, "w": target_w, "h": target_h,
               "cache_key": os.path.basename(os.path.dirname(src)),
               "context": context or {}, "role": role}
        if enqueue_comfy(cfg, job):
            log(f"  [up] game is running — deferred {os.path.basename(src)} "
                "to the ComfyUI queue (runs when you stop playing)")
        return None
    wf, err = validate_workflow(_workflow_text(c, role))
    if err:
        log(f"  [up] comfy {role + ' ' if role else ''}workflow problem: {err}")
        return None
    try:
        base = _comfy_base(cfg)
    except ValueError as e:
        log(f"  [up] comfy config: {e}")
        return None
    try:
        timeout_s = max(60, int(c.get("timeout_seconds") or 900))
    except (TypeError, ValueError):
        timeout_s = 900
    try:
        # 1) upload the source image under a unique name
        ext = os.path.splitext(src)[1].lower() or ".png"
        up_name = f"steamtheme_{uuid.uuid4().hex}{ext}"
        with open(src, "rb") as f:
            r = requests.post(base + "/upload/image", timeout=120,
                              data={"overwrite": "true"},
                              files={"image": (up_name, f)})
        if r.status_code != 200:
            log(f"  [up] comfy upload failed: HTTP {r.status_code} "
                f"{r.text[:200]}")
            return None
        uploaded = r.json().get("name") or up_name

        # 2) patch the I/O nodes, fill {placeholders} everywhere, submit
        token = uuid.uuid4().hex[:8]
        for node in wf.values():
            if not isinstance(node, dict):
                continue
            ct = node.get("class_type")
            if ct == "LoadImage":
                node.setdefault("inputs", {})["image"] = uploaded
            elif ct in ("SaveImage", "Save Image"):
                node.setdefault("inputs", {})["filename_prefix"] = \
                    f"steamtheme_{token}"
        ctx = dict(context or {})
        if role:
            ctx.setdefault("role", role)
        wf, dropped = fill_workflow(wf, ctx)
        if dropped:
            log(f"  [up] unknown workflow placeholders ignored: "
                f"{', '.join(sorted(set(dropped)))}")
        r = requests.post(base + "/prompt", timeout=60, json={"prompt": wf})
        if r.status_code != 200:
            log(f"  [up] comfy submit failed: HTTP {r.status_code} "
                f"{r.text[:200]}")
            return None
        pid = r.json().get("prompt_id")
        if not pid:
            log(f"  [up] comfy submit: no prompt_id in {r.text[:200]}")
            return None
        log(f"  [up] comfy job {str(pid)[:8]} submitted")

        # 3) poll history until the job shows up with outputs
        deadline = time.time() + timeout_s
        entry = None
        while time.time() < deadline:
            time.sleep(3)
            h = requests.get(f"{base}/history/{pid}", timeout=30)
            if h.status_code != 200:
                continue
            entry = (h.json() or {}).get(pid)
            if entry:
                break
        if not entry:
            log("  [up] comfy timed out")
            return None
        st = entry.get("status") or {}
        if st.get("status_str") == "error" or st.get("completed") is False:
            msgs = "; ".join(
                str(m[1].get("exception_message", "?"))
                for m in (st.get("messages") or [])
                if isinstance(m, (list, tuple)) and len(m) > 1
                and isinstance(m[1], dict))[:200]
            log(f"  [up] comfy job failed: {msgs or st.get('status_str')}")
            return None
        images = []
        for out in (entry.get("outputs") or {}).values():
            if isinstance(out, dict):
                images += [im for im in out.get("images") or []
                           if isinstance(im, dict) and im.get("filename")]
        if not images:
            log("  [up] comfy finished but produced no image")
            return None
        im = images[-1]  # the SaveImage output
        dl = requests.get(base + "/view", timeout=300,
                          params={"filename": im["filename"],
                                  "subfolder": im.get("subfolder", ""),
                                  "type": im.get("type", "output")})
        if dl.status_code != 200:
            log(f"  [up] comfy download failed: HTTP {dl.status_code}")
            return None
        import io
        try:
            Image.open(io.BytesIO(dl.content)).verify()
        except Exception:
            log("  [up] comfy download: not an image")
            return None
        with open(dst, "wb") as f:
            f.write(dl.content)
        return dst
    except requests.RequestException as e:
        log(f"  [up] comfy unreachable: {e}")
        return None
    except Exception as e:
        log(f"  [up] comfy error: {e}")
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
    prov = up.get("provider")
    model = (_model_for(cfg.get("topaz", {}), role) if prov == "topaz"
             else _comfy_slug(cfg, role) if prov == "comfy" else "ai")
    dst = cache_path(path, cfg, role)
    if os.path.exists(dst):
        return dst  # one upscale per source+model; never re-spend credits
    log(f"  [up] {os.path.basename(path)} is {w}x{h}, target {target_w}x{target_h}, "
        f"model '{model}' — upscaling")
    if prov == "topaz":
        out = _topaz(path, dst, cfg, log, target_w, target_h, context, role)
    elif prov == "comfy":
        out = _comfy(path, dst, cfg, log, target_w, target_h, context,
                     role=role)
    else:
        out = _ai(path, dst, cfg, log)
    if out:
        with Image.open(out) as im:
            log(f"  [up] done: {im.size[0]}x{im.size[1]} -> {out}")
        return out
    log("  [up] falling back to original (PIL will stretch it instead)")
    return path
