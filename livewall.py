"""Optional live wallpaper: a looping video generated from the hero art.

Provider "comfy" (local ComfyUI): the hero image is uploaded to ComfyUI, a
user-supplied (or built-in default) workflow generates a short looping
video — LTX-2.5 first/last-frame conditioning in the default, with an
optional Topaz video-upscale node — and the result is installed as a
Wallpaper Engine animated background on the hero display.

Workflow formats accepted:
  - classic Export-API JSON (flat numbered nodes with class_type)
  - the new ComfyUI graph/save format (nodes/links/definitions, incl.
    subgraphs) — flattened to API form automatically, so a normal
  workflow save pastes straight in.

{placeholders} from the AI/VLM node ({game}, {mood}, {theme_name},
{appearance}, {palette_mode}, {role} plus custom naming-prompt keys) are
filled into every workflow string, JSON-safely, exactly like the upscaling
node.

Videos are expensive (minutes of GPU), so:
  - defer_while_gaming (default on) queues generation while a game runs
  - results are cached per hero-image content hash; a cached video is
    never re-generated unless its cache folder is deleted
Wallpaper Engine apply is Windows-only; on other platforms the video is
still generated and cached.
"""

import glob
import hashlib
import json
import os
import subprocess
import threading
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PATH = os.path.join(BASE_DIR, "livewall_default_workflow.json")

# widgets the frontend saves that are not real API inputs
_FRONTEND_ONLY_WIDGETS = {"upload", "control_after_generate",
                          "control_before_generate", "image_upload"}
# output nodes that persist a playable video file
_VIDEO_SAVERS = {"SaveVideo", "VHS_VideoCombine", "SaveAnimatedWEBP",
                 "SaveAnimatedPNG", "SaveAnimatedGIF"}
_VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".gif")
_HOST_RE = __import__("re").compile(r"^[A-Za-z0-9.\-]+$")
_queue_lock = threading.Lock()


def default_workflow():
    """The built-in default workflow (graph format, served to the UI as an
    editable guide). Loaded from livewall_default_workflow.json so the file
    stays a normal JSON asset, not a giant string literal."""
    with open(_DEFAULT_PATH, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- workflow

def _base(cfg):
    c = cfg.get("livewall", {})
    host = str(c.get("host") or "127.0.0.1").strip()
    if not _HOST_RE.fullmatch(host):
        raise ValueError(f"bad live-wallpaper host {host!r}")
    try:
        port = int(c.get("port") or 8188)
    except (TypeError, ValueError):
        port = 8188
    if not 1 <= port <= 65535:
        raise ValueError(f"bad live-wallpaper port {port!r}")
    return f"http://{host}:{port}"


def graph_to_api(graph, errors=None):
    """Convert a ComfyUI graph-format save (nodes/links/definitions, with
    subgraph expansion) into the classic flat Export-API dict. Returns
    (api_nodes or None, errors list)."""
    errors = errors if errors is not None else []
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        errors.append("not a graph-format workflow (missing nodes list)")
        return None, errors
    if not all(isinstance(l, (list, tuple)) and len(l) >= 5
               for l in graph.get("links") or []):
        errors.append("graph-format workflow has malformed links")
        return None, errors
    outer_links = {l[0]: l for l in graph.get("links") or []}
    sg_defs = {s.get("id"): s
               for s in (graph.get("definitions", {}) or {})
               .get("subgraphs", []) or [] if isinstance(s, dict)}
    api = {}
    # (subgraph_instance_id, output_slot) -> [api_node_id, api_output_slot]
    sub_out = {}

    def resolve_outer(lid):
        l = outer_links.get(lid)
        if not l:
            errors.append(f"unresolved link id {lid}")
            return None
        oid, oslot = l[1], l[2]
        if oid in sub_out:
            return sub_out[oid].get(oslot)
        return [str(oid), oslot]

    def expand_subgraph(sg, instance, prefix):
        sgl = {l.get("id"): l for l in sg.get("links") or []}
        sgn = {n.get("id"): n for n in sg.get("nodes") or []}
        # resolve each boundary input BY NAME against the instance — the
        # instance's inputs[] is only a SUBSET of the boundary (widget-only
        # inputs may not be materialized there), so never fall back to
        # positional matching
        gin = instance.get("inputs") or []
        by_name = {e.get("name"): e for e in gin if isinstance(e, dict)}
        named = instance.get("widgets_values_named") or {}
        boundary = {}
        for i, sgi in enumerate(sg.get("inputs") or []):
            nm = sgi.get("name")
            entry = by_name.get(nm)
            if entry is not None and entry.get("link") is not None:
                val = resolve_outer(entry.get("link"))
            else:
                val = named.get(nm)
            if val is None:
                errors.append(f"subgraph input {nm!r} has no source "
                               "(link or widget value)")
                continue
            boundary[i] = val
        for t in sg.get("nodes") or []:
            if t.get("mode", 0) != 0:
                continue
            if t.get("type") in sg_defs:
                errors.append("nested subgraphs are not supported yet")
                continue
            tid = f"{prefix}{t['id']}"
            named_t = dict(t.get("widgets_values_named") or {})
            tin = t.get("inputs") or []
            inputs = {}
            in_by_slot = {}
            for l in sg.get("links") or []:
                if l.get("origin_id") == -10:
                    in_by_slot.setdefault(l.get("target_id"), []).append(l)
            boundary_here = in_by_slot.get(t.get("id")) or []
            for l in boundary_here:
                tslot = l.get("target_slot")
                entry = tin[tslot] if tslot < len(tin) else None
                if not isinstance(entry, dict):
                    continue
                val = boundary.get(l.get("origin_slot"))
                if val is not None:
                    inputs[entry.get("name")] = val
                    named_t.pop(entry.get("name"), None)
            for entry in tin:
                if not isinstance(entry, dict) or entry.get("link") is None:
                    continue
                l = sgl.get(entry.get("link"))
                if l is None:
                    continue
                oid, oslot = l.get("origin_id"), l.get("origin_slot")
                if oid == -10:
                    val = boundary.get(oslot)
                else:
                    val = [f"{prefix}{oid}", oslot]
                if val is not None:
                    inputs[entry.get("name")] = val
                    named_t.pop(entry.get("name"), None)
            for k, v in named_t.items():
                if k in _FRONTEND_ONLY_WIDGETS or k in inputs:
                    continue
                inputs[k] = v
            api[tid] = {"class_type": t.get("type"), "inputs": inputs}
            if t.get("title"):
                api[tid]["_meta"] = {"title": t["title"]}
        sub_out[instance.get("id")] = {}
        for j, sgo in enumerate(sg.get("outputs") or []):
            for lid in sgo.get("linkIds") or []:
                l = sgl.get(lid)
                if l and l.get("target_id") == -20:
                    sub_out[instance.get("id")][j] = \
                        [f"{prefix}{l.get('origin_id')}", l.get("origin_slot")]

    # pass 1: expand subgraph instances (fills sub_out for link resolution)
    for gnode in graph.get("nodes") or []:
        if gnode.get("type") in sg_defs:
            expand_subgraph(sg_defs[gnode["type"]], gnode, f"{gnode['id']}_")
    # pass 2: plain top-level nodes (outer links; subgraph outputs via sub_out)
    for gnode in graph.get("nodes") or []:
        if gnode.get("mode", 0) != 0 or gnode.get("type") in sg_defs:
            continue
        gin = gnode.get("inputs") or []
        named = dict(gnode.get("widgets_values_named") or {})
        inputs = {}
        for entry in gin:
            if not isinstance(entry, dict) or entry.get("link") is None:
                continue
            val = resolve_outer(entry.get("link"))
            if val is not None:
                inputs[entry.get("name")] = val
                named.pop(entry.get("name"), None)
        for k, v in named.items():
            if k in _FRONTEND_ONLY_WIDGETS or k in inputs:
                continue
            inputs[k] = v
        nid = str(gnode.get("id"))
        api[nid] = {"class_type": gnode.get("type"), "inputs": inputs}
        if gnode.get("title"):
            api[nid]["_meta"] = {"title": gnode["title"]}
    return api, errors


def _ensure_video_output(api):
    """Append CreateVideo + SaveVideo wired to the terminal IMAGE output
    when the workflow has no video-saving node. Returns error or None."""
    def class_types():
        return {str(n.get("class_type")) for n in api.values()}
    types = class_types()
    if types & _VIDEO_SAVERS:
        return None
    # TopazVideoAI saves the video itself when save_video is on
    for nid, n in api.items():
        if n.get("class_type") == "TopazVideoAI" and \
                n.get("inputs", {}).get("save_video"):
            return None
    # find an IMAGE output nobody consumes (the terminal frames source).
    # Savers are excluded (their outputs aren't frames); everything else is
    # fair game — GetVideoComponents.images and even a bare LoadImage work.
    consumed = {tuple(v) for n in api.values()
                for v in n.get("inputs", {}).values()
                if isinstance(v, list) and len(v) == 2
                and isinstance(v[0], str) and isinstance(v[1], int)}
    _image_savers = {"SaveImage", "PreviewImage", "SaveImageWebsocket",
                     "SaveImageOutput", "ImageOnlySave"}
    # GetVideoComponents carries the source fps on output slot 2 — prefer it
    fps_src = None
    for nid, n in api.items():
        if n.get("class_type") == "GetVideoComponents" \
                and (nid, 2) not in consumed:
            fps_src = [nid, 2]
    # slot 0 only: it is IMAGE for LoadImage/GetVideoComponents and the
    # primary output for essentially every frames producer — probing higher
    # slots would grab side outputs like LoadImage's MASK
    terminal = None
    for nid, n in api.items():
        if n.get("class_type") in _VIDEO_SAVERS | _image_savers:
            continue
        if (nid, 0) not in consumed:
            terminal = [nid, 0]
            break
    if not terminal:
        return ("workflow produces no video: add a SaveVideo node (or point "
                "the final IMAGE output at one)")
    create = {"class_type": "CreateVideo",
              "inputs": {"images": terminal,
                         "fps": fps_src if fps_src else 24.0}}
    api["steamtheme_create"] = create
    api["steamtheme_save"] = {"class_type": "SaveVideo",
                              "inputs": {"video": ["steamtheme_create", 0],
                                         "filename_prefix":
                                             "steamtheme/loop"}}
    return None


def parse_workflow(text):
    """Parse the configured workflow text into a flat API dict, accepting
    both the API format and the graph (save) format. Returns
    (api_dict or None, error or None)."""
    raw = (text or "").strip()
    if not raw:
        raw = default_workflow()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if isinstance(obj, dict) and isinstance(obj.get("nodes"), list):
        api, errs = graph_to_api(obj)
        if not api:
            return None, "graph conversion failed: " + "; ".join(errs[:4])
        if errs:
            return None, "graph conversion failed: " + "; ".join(errs[:4])
    elif isinstance(obj, dict) and obj:
        api = {}
        for nid, n in obj.items():
            if not isinstance(n, dict) or "class_type" not in n:
                return None, f"node {nid}: missing class_type"
            api[str(nid)] = {"class_type": n.get("class_type"),
                             "inputs": dict(n.get("inputs") or {})}
            if n.get("_meta"):
                api[str(nid)]["_meta"] = n["_meta"]
    else:
        return None, "workflow must be an Export-API JSON object or a " \
                    "ComfyUI graph-format save"
    types = {str(n.get("class_type")) for n in api.values()}
    if "LoadImage" not in types:
        return None, "missing a LoadImage node (the hero image to animate)"
    err = _ensure_video_output(api)
    if err:
        return None, err
    return api, None


def validate_workflow(text):
    """Structural validation for the UI Test button. Returns
    (api_or_None, error_or_None)."""
    api, err = parse_workflow(text)
    return api, err


# ---------------------------------------------------------------- queue

def _queue_file(cfg):
    cache_root = cfg.get("cache_dir") or "cache"
    return os.path.join(cache_root, "video_queue.json")


def _game_running(cfg):
    import upscale
    return upscale._game_running(cfg)


def enqueue(cfg, job):
    """Queue a deferred video job (deduped by dst). Atomic write."""
    path = _queue_file(cfg)
    with _queue_lock:
        try:
            with open(path, encoding="utf-8") as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            q = []
        if not isinstance(q, list):
            q = []
        if any(isinstance(j, dict) and j.get("dst") == job["dst"] for j in q):
            return False
        q.append(job)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, path)
    return True


def drain_queue(cfg, log=print):
    """Run queued video jobs once the GPU is free. Returns [(cache_key,
    theme_title, project_path)] for finished jobs. Never raises."""
    lw = cfg.get("livewall", {})
    if not (lw.get("enabled") and lw.get("provider") == "comfy"):
        return []
    with _queue_lock:
        try:
            with open(_queue_file(cfg), encoding="utf-8") as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
    if not isinstance(q, list) or not q:
        return []
    if lw.get("defer_while_gaming", True) and _game_running(cfg):
        return []
    try:
        r = requests.get(_base(cfg) + "/system_stats", timeout=5)
        if r.status_code != 200:
            raise requests.RequestException("bad status")
    except Exception:
        log(f"  [livewall] {len(q)} video job(s) queued but ComfyUI is "
            "unreachable")
        return []
    done, remaining = [], []
    for job in q:
        out = None
        try:
            out = generate(cfg, job["src"], job["dst"], job.get("context"),
                           log, defer=False)
        except Exception as e:
            log(f"  [livewall] queue job error: {e}")
        if out:
            proj = install(cfg, job["dst"], job.get("cache_key") or "",
                          job.get("theme_title") or "", job.get("hero") or "",
                          log)
            done.append((job.get("cache_key") or "", job.get("theme_title"),
                         proj))
        else:
            job["attempts"] = int(job.get("attempts") or 0) + 1
            if job["attempts"] >= 3:
                log("  [livewall] dropping video job after 3 failed attempts")
            else:
                remaining.append(job)
    with _queue_lock:
        tmp = _queue_file(cfg) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(remaining, f, indent=2)
        os.replace(tmp, _queue_file(cfg))
    return done


# ---------------------------------------------------------------- generate

def generate(cfg, hero_path, dst, context, log=print, defer=True):
    """Generate the looping video for hero_path into dst via ComfyUI.
    Deferred while gaming (enqueue) unless defer=False. Never raises.
    Returns dst on success, else None."""
    import upscale
    lw = cfg.get("livewall", {})
    if defer and lw.get("defer_while_gaming", True) and _game_running(cfg):
        job = {"src": hero_path, "dst": dst, "context": context or {},
               "cache_key": None, "theme_title": None, "hero": hero_path}
        if enqueue(cfg, job):
            log(f"  [livewall] game is running — video queued for "
                f"{os.path.basename(hero_path)} (generated when you stop)")
        return None
    api, err = parse_workflow(lw.get("workflow") or "")
    if err:
        log(f"  [livewall] workflow problem: {err}")
        return None
    try:
        base = _base(cfg)
    except ValueError as e:
        log(f"  [livewall] config: {e}")
        return None
    try:
        t_out = int(lw.get("timeout_seconds") or 3600)
    except (TypeError, ValueError):
        t_out = 3600
    t_out = max(60, t_out)
    try:
        # 1) filter widget constants against the server's real input specs
        classes = sorted({str(n.get("class_type")) for n in api.values()})
        spec = {}
        for ct in classes:
            try:
                r = requests.get(f"{base}/object_info/{ct}", timeout=20)
                if r.status_code == 200:
                    spec[ct] = r.json().get(ct) or {}
            except requests.RequestException:
                pass
        for nid, n in api.items():
            sp = (spec.get(str(n.get("class_type")) or {}) or {}).get(
                "input") or {}
            allowed = set(sp.get("required") or {}) | set(sp.get("optional") or {})
            if not allowed:
                continue
            # dotted widget names (resize_type.crop, sampling_mode.seed)
            # are dynamic-combo sub-values and ARE valid API inputs — keep
            # them (with the bare combo key holding the selection) whenever
            # the prefix is a known input; only genuinely unknown names go.
            # Probed live against ComfyUI 0.42.5: flat dotted executes; the
            # nested {"key", "inputs"} shape fails inside the node.
            for k in list(n.get("inputs", {})):
                v = n["inputs"][k]
                if isinstance(v, list) or k in allowed:
                    continue
                prefix, dot, _sub = k.partition(".")
                if dot and prefix in allowed:
                    continue
                log(f"  [livewall] node {nid}: dropped unknown input "
                    f"{k!r} (not a server input)")
                n["inputs"].pop(k)
        # 2) upload the hero; every LoadImage node becomes the hero so the
        #    first and last frames match -> the video loops back to the art
        ext = os.path.splitext(hero_path)[1].lower() or ".jpg"
        up_name = f"steamtheme_{hashlib.sha1(dst.encode()).hexdigest()[:12]}{ext}"
        with open(hero_path, "rb") as f:
            r = requests.post(base + "/upload/image", timeout=120,
                              data={"overwrite": "true"},
                              files={"image": (up_name, f)})
        if r.status_code != 200:
            log(f"  [livewall] upload failed: HTTP {r.status_code} "
                f"{r.text[:200]}")
            return None
        for nid, n in api.items():
            if n.get("class_type") == "LoadImage":
                n.setdefault("inputs", {})["image"] = up_name
            elif n.get("class_type") == "SaveVideo":
                n["inputs"]["filename_prefix"] = "steamtheme/" + up_name[:24]
        # 3) placeholders (post-parse, JSON-safe)
        ctx = dict(context or {})
        ctx.setdefault("role", "hero")
        api, dropped = upscale.fill_workflow(api, ctx)
        if dropped:
            log(f"  [livewall] unknown workflow placeholders ignored: "
                f"{', '.join(sorted(set(dropped)))}")
        # 4) submit + poll
        r = requests.post(base + "/prompt", timeout=60, json={"prompt": api})
        if r.status_code != 200:
            log(f"  [livewall] submit failed: HTTP {r.status_code} "
                f"{r.text[:300]}")
            return None
        pid = r.json().get("prompt_id")
        if not pid:
            log(f"  [livewall] submit: no prompt_id ({r.text[:200]})")
            return None
        log(f"  [livewall] video job {str(pid)[:8]} submitted — this takes "
            "minutes; polling")
        deadline = time.time() + t_out
        entry = None
        while time.time() < deadline:
            time.sleep(5)
            h = requests.get(f"{base}/history/{pid}", timeout=30)
            if h.status_code != 200:
                continue
            entry = (h.json() or {}).get(pid)
            if entry:
                break
        if not entry:
            log("  [livewall] video job timed out")
            return None
        st = entry.get("status") or {}
        if st.get("status_str") == "error" or st.get("completed") is False:
            msgs = "; ".join(str(m[1].get("exception_message", "?"))
                            for m in (st.get("messages") or [])
                            if isinstance(m, (list, tuple)) and len(m) > 1
                            and isinstance(m[1], dict))[:300]
            log(f"  [livewall] video job failed: {msgs or st.get('status_str')}")
            return None
        # 5) find the saved video file in the outputs (any saver node)
        found = []

        def scan(v):
            if isinstance(v, dict):
                fn = v.get("filename")
                if isinstance(fn, str) and fn.lower().endswith(_VIDEO_EXTS):
                    found.append((fn, v.get("subfolder", ""),
                                  v.get("type", "output")))
                for x in v.values():
                    scan(x)
            elif isinstance(v, list):
                for x in v:
                    scan(x)

        scan(entry.get("outputs") or {})
        if not found:
            log("  [livewall] finished but produced no video file")
            return None
        fn, sub, typ = found[-1]
        dl = requests.get(base + "/view", timeout=600,
                          params={"filename": fn, "subfolder": sub,
                                  "type": typ})
        if dl.status_code != 200 or len(dl.content) < 1024:
            log(f"  [livewall] video download failed: HTTP {dl.status_code}")
            return None
        if b"ftyp" not in dl.content[:16] and not fn.lower().endswith(".gif"):
            log("  [livewall] download was not a video file")
            return None
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(dl.content)
        log(f"  [livewall] video ready ({len(dl.content) // 1024} KB) -> {dst}")
        return dst
    except requests.RequestException as e:
        log(f"  [livewall] ComfyUI unreachable: {e}")
        return None
    except Exception as e:
        log(f"  [livewall] error: {e}")
        return None


# ---------------------------------------------------------- install + WE

def install(cfg, video_path, cache_key, theme_title, hero_path, log=print):
    """Wrap the generated video as a Wallpaper Engine project (project.json
    + preview) next to it. Returns the project.json path."""
    import extras
    d = os.path.dirname(video_path)
    preview = os.path.join(d, "preview.jpg")
    try:
        from PIL import Image
        with Image.open(hero_path) as im:
            im.convert("RGB").save(preview, "JPEG", quality=80)
    except Exception:
        preview = ""
    title = extras._safe_title_name(
        (theme_title or "Steam Theme")) + " - " + extras._safe_title_name(
        _game_name(cfg, cache_key))
    project = {"file": os.path.basename(video_path), "type": "video",
               "title": title,
               "description": "Generated by Steam Theme",
               "tags": ["Game", "Steam Theme"],
               "general": {"properties": {}}}
    if preview:
        project["preview"] = "preview.jpg"
    ppath = os.path.join(d, "project.json")
    with open(ppath, "w", encoding="utf-8") as f:
        json.dump(project, f, indent=2)
    return ppath


def _game_name(cfg, cache_key):
    """Best-effort game name for the WE title."""
    pal = os.path.join(cfg.get("cache_dir") or "cache",
                       str(cache_key), "palette.json")
    try:
        with open(pal, encoding="utf-8") as f:
            return json.load(f).get("game_name") or str(cache_key)
    except (OSError, ValueError):
        return str(cache_key)


def we_binary():
    """Wallpaper Engine's control binary, or None when not installed."""
    roots = [r"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine",
             r"C:\Program Files\Steam\steamapps\common\wallpaper_engine"]
    # Steam library folders via libraryfolders.vdf
    vdf = r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf"
    try:
        with open(vdf, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"path"' in line:
                    p = line.split('"path"')[1].strip().strip('"') \
                             .replace("\\\\", "\\")
                    roots.insert(0, os.path.join(p, "steamapps", "common",
                                                  "wallpaper_engine"))
    except OSError:
        pass
    for r in roots:
        for exe in ("wallpaper64.exe", "wallpaper32.exe"):
            if os.path.isfile(os.path.join(r, exe)):
                return os.path.join(r, exe)
    return None


def we_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq wallpaper64.exe"],
                             capture_output=True, text=True, timeout=10)
        if "wallpaper64" in (out.stdout or ""):
            return True
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq wallpaper32.exe"],
                              capture_output=True, text=True, timeout=10)
        return "wallpaper32" in (out.stdout or "")
    except Exception:
        return False


def we_monitor(cfg, hero_device):
    r"""The Wallpaper Engine monitor index. Config override
    (livewall.we_monitor, an int) wins; otherwise the hero display's
    Windows index (\\.\DISPLAY5 -> 4)."""
    lw = cfg.get("livewall", {})
    try:
        m = int(lw.get("we_monitor"))
        if m >= 0:
            return m
    except (TypeError, ValueError):
        pass
    d = str(hero_device or "")
    import re
    mm = re.search(r"DISPLAY(\d+)", d)
    if mm:
        return max(0, int(mm.group(1)) - 1)
    return 0


def apply_we(cfg, project_path, hero_device, log=print):
    """Set the generated wallpaper video in Wallpaper Engine, live via
    -control when it's running (it updates its own config); otherwise by
    editing its config.json so it's active on the next WE launch."""
    if os.name != "nt":
        log("  [livewall] Wallpaper Engine is Windows-only; video is cached "
            "for later use")
        return False
    exe = we_binary()
    if not exe:
        log("  [livewall] Wallpaper Engine not found; video is cached for "
            "later use")
        return False
    idx = we_monitor(cfg, hero_device)
    if we_running():
        try:
            r = subprocess.run([exe, "-control", "openWallpaper",
                                "-file", os.path.abspath(project_path),
                                "-monitor", str(idx)],
                               capture_output=True, text=True, timeout=30)
            log(f"  [livewall] applied to Wallpaper Engine monitor {idx} "
                f"(exit {r.returncode})")
            return r.returncode == 0
        except Exception as e:
            log(f"  [livewall] WE control failed: {e}")
            return False
    # not running: patch config.json (WE will load it on next start)
    cfgp = os.path.join(os.path.dirname(exe), "config.json")
    try:
        with open(cfgp, encoding="utf-8", errors="replace") as f:
            c = json.load(f)
        user = c.get(os.environ.get("USERNAME") or "")
        if not user:
            return False
        sel = (user.setdefault("general", {})
               .setdefault("wallpaperconfig", {})
               .setdefault("selectedwallpapers", {}))
        sel[f"Monitor{idx}"] = {"file": os.path.abspath(project_path)
                                .replace("\\", "/")}
        tmp = cfgp + ".steamtheme.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=0)
        os.replace(tmp, cfgp)
        log(f"  [livewall] Wallpaper Engine isn't running — queued for "
            f"monitor {idx} on its next launch")
        return True
    except Exception as e:
        log(f"  [livewall] could not update Wallpaper Engine config: {e}")
        return False


# ------------------------------------------------------------ orchestration

def reapply_cached(cfg, cache_key, pal, hero_device, log=print):
    """Gallery re-apply: hand Wallpaper Engine the newest cached video for
    this theme (or generate one when the feature is on and none exists
    yet). pal is the cached palette.json."""
    lw = cfg.get("livewall", {})
    if not (lw.get("enabled") and lw.get("provider") == "comfy"):
        return
    root = os.path.join(cfg.get("cache_dir") or "cache",
                        str(cache_key), "livewall")
    if os.path.isdir(root):
        projs = glob.glob(os.path.join(root, "*", "project.json"))
        if projs:
            newest = max(projs, key=os.path.getmtime)
            apply_we(cfg, newest, hero_device, log)
            return
    hero = None
    try:
        art = artwork_from_cache(os.path.join(
            cfg.get("cache_dir") or "cache", str(cache_key)))
        hero = (art or {}).get("hero")
    except Exception:
        pass
    if not hero:
        return
    ctx = {"game": (pal or {}).get("game_name") or str(cache_key),
           "theme_name": (pal or {}).get("theme_name") or "",
           "mood": (pal or {}).get("mood") or "",
           "appearance": (pal or {}).get("appearance") or "dark",
           "palette_mode": (pal or {}).get("mode") or "colorful"}
    maybe_generate(cfg, hero, ctx, cache_key,
                   (pal or {}).get("theme_name") or str(cache_key),
                   hero_device, log)


def artwork_from_cache(cache_dir):
    import artwork
    return artwork.art_from_cache(cache_dir)


def maybe_generate(cfg, hero, context, cache_key, theme_title, hero_device,
                   log=print):
    """Called from the theme pipeline: generate (or reuse) the looping video
    for the current hero and hand it to Wallpaper Engine."""
    lw = cfg.get("livewall", {})
    if not (lw.get("enabled") and lw.get("provider") == "comfy"):
        return
    if not hero or not os.path.exists(hero):
        return
    try:
        with open(hero, "rb") as f:
            h = hashlib.sha1(f.read()).hexdigest()[:10]
    except OSError:
        return
    proj_dir = os.path.join(cfg.get("cache_dir") or "cache",
                            str(cache_key), "livewall", h)
    dst = os.path.join(proj_dir, "wallpaper.mp4")
    if os.path.exists(dst):
        proj = install(cfg, dst, cache_key, theme_title, hero, log)
        apply_we(cfg, proj, hero_device, log)
        return
    job = {"src": hero, "dst": dst, "context": context or {},
           "cache_key": cache_key, "theme_title": theme_title, "hero": hero}
    out = generate(cfg, hero, dst, context, log)
    if out:
        proj = install(cfg, dst, cache_key, theme_title, hero, log)
        apply_we(cfg, proj, hero_device, log)
    # generate() enqueues when deferred; annotate the queued job so the
    # drain can install + apply it with full context. Only ONE video job
    # per game is kept — a refetch may change the hero (and its content
    # hash), and only the newest job describes the current art.
    with _queue_lock:
        try:
            with open(_queue_file(cfg), encoding="utf-8") as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            q = []
        q = [j for j in q if isinstance(j, dict)
             and not (j.get("cache_key") == cache_key
                      and j.get("dst") != dst)]
        for j in q:
            if j.get("dst") == dst:
                j.update({"cache_key": cache_key,
                          "theme_title": theme_title})
        tmp = _queue_file(cfg) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, _queue_file(cfg))