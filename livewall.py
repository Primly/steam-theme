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

Every submitted job also carries an editable *graph* in ComfyUI's history
metadata (extra_data.extra_pnginfo.workflow): a configured graph save is
embedded verbatim, while API-format workflows are synthesized into a graph
(api_to_graph, using /object_info for slot order) — so "Open as workflow in
a new tab" always has a real graph to restore.

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
import uuid

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
_draining = False  # a background drain worker is running


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


_BARE_PLACEHOLDER = __import__("re").compile(
    r'\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}')


def repair_json(text):
    """Best-effort repair of a hand-edited workflow: people type
    {placeholder} tokens straight into the JSON (e.g. `{loop_prompt},` in
    widgets_values) and JSON needs them quoted.

    String-aware scan: tokens OUTSIDE a JSON string are wrapped in quotes;
    anything inside a string is left exactly as typed (a prompt like
    "{game} key art, {mood}" must survive untouched). Only bare
    single-identifier braces match — real objects contain ':' or quotes.
    Returns (text, n_repairs)."""
    out, i, n = [], 0, 0
    in_str = esc = False
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        m = _BARE_PLACEHOLDER.match(text, i)
        if m:
            out.append(f'"{{{m.group(1)}}}"')
            n += 1
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out), n


def parse_workflow(text, log=None, topaz_finish=True):
    """Parse the configured workflow text into a flat API dict, accepting
    both the API format and the graph (save) format. Returns
    (api_dict or None, error or None).

    topaz_finish=False removes any TopazVideoAI node so the raw diffusion
    frames flow straight into a CreateVideo/SaveVideo (no local Topaz Video
    AI license required)."""
    raw = (text or "").strip()
    if not raw:
        raw = default_workflow()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        fixed, n = repair_json(raw)
        if not n:
            return None, f"invalid JSON: {e}"
        try:
            obj = json.loads(fixed)
        except json.JSONDecodeError:
            return None, f"invalid JSON: {e}"
        if log:
            log(f"  [livewall] workflow JSON had {n} unquoted "
                "{placeholder} token(s) — auto-quoted them")
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
    if not topaz_finish:
        stripped = [nid for nid, n in api.items()
                    if n.get("class_type") == "TopazVideoAI"]
        for nid in stripped:
            api.pop(nid)
        if stripped and log:
            log(f"  [livewall] Topaz finish off — bypassed "
                f"{len(stripped)} TopazVideoAI node(s)")
    err = _ensure_video_output(api)
    if err:
        return None, err
    return api, None


def validate_workflow(text):
    """Structural validation for the UI Test button. Returns
    (api_or_None, error_or_None)."""
    api, err = parse_workflow(text)
    return api, err


# ------------------------------------------------------- graph synthesis

_OBJECT_INFO_CACHE = {}   # server URL -> (fetched_at, node definitions)
_OBJECT_INFO_TTL = 600
_WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def fetch_object_info(base, timeout=20):
    """ComfyUI's node definitions (/object_info): input order, slot types and
    widget defaults. Used to synthesize a faithful graph from an API prompt.
    Cached per server for 10 minutes; returns {} when unavailable (synthesis
    still works, just with less precise slot ordering)."""
    cached = _OBJECT_INFO_CACHE.get(base)
    now = time.time()
    if cached and now - cached[0] < _OBJECT_INFO_TTL:
        return cached[1]
    try:
        r = requests.get(base + "/object_info", timeout=timeout)
        if r.status_code == 200 and isinstance(r.json(), dict):
            _OBJECT_INFO_CACHE[base] = (now, r.json())
            return _OBJECT_INFO_CACHE[base][1]
    except Exception:
        pass
    return cached[1] if cached else {}


def _input_specs(defs, class_type):
    """[(name, slot_type, opts, is_widget, raw_type)] in definition order."""
    info = (defs or {}).get(class_type) or {}
    inp = info.get("input") or {}
    specs = []
    for section in ("required", "optional"):
        for name, spec in (inp.get(section) or {}).items():
            raw = spec[0] if isinstance(spec, (list, tuple)) and spec else "*"
            opts = (spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1
                    else {})
            if not isinstance(opts, dict):
                opts = {}
            if opts.get("forceInput"):
                widget = False
            elif isinstance(raw, list):
                widget = True
            else:
                t = str(raw).upper()
                # newer ComfyUI "dynamic combo" inputs (e.g.
                # COMFY_DYNAMICCOMBO_V3) behave like widgets
                widget = t in _WIDGET_TYPES or "DYNAMICCOMBO" in t
            slot_type = raw if isinstance(raw, str) else "COMBO"
            specs.append((name, slot_type, opts, widget, raw))
    return specs


def _typed_default(raw_type):
    """A safe non-null widget default for a ComfyUI input type."""
    if isinstance(raw_type, list):
        return raw_type[0] if raw_type else ""
    t = str(raw_type).upper()
    if t == "INT":
        return 0
    if t == "FLOAT":
        return 0.0
    if t == "BOOLEAN":
        return False
    return ""


def _widget_value(has_val, val, raw_type, opts):
    """Resolve one widget value, never yielding None (the graph schema
    rejects null widget values)."""
    if has_val and val is not None:
        return val
    if "default" in opts and opts["default"] is not None:
        return opts["default"]
    return _typed_default(raw_type)


def _is_ref(val, api):
    """API value is a link: [source_node, output_slot]."""
    return (isinstance(val, list) and len(val) == 2
            and not isinstance(val[1], (list, dict))
            and str(val[0]) in api)


def _map_ids(api):
    """Graph node ids: numeric strings -> ints, 'group:node' -> 'group_node'
    (deduped) — mirroring ComfyUI's own API-JSON loader."""
    ids, used = {}, {k for k in api if ":" not in str(k)}
    for raw in api:
        s = str(raw)
        if ":" not in s:
            ids[raw] = int(s) if s.lstrip("-").isdigit() else s
            continue
        cand = s.replace(":", "_")
        while cand in used:
            cand += "_"
        used.add(cand)
        ids[raw] = cand
    return ids


def api_to_graph(api, defs=None):
    """Turn a flat Export-API prompt into a ComfyUI graph-format save
    (nodes/links/version) so a submitted job can be opened as an editable
    workflow from ComfyUI's history.

    Node definitions (/object_info, optional) give exact input order, slot
    types and widget defaults; without them, linked values become input
    slots in API order and the rest become widget values."""
    if not isinstance(api, dict) or not api:
        return None
    defs = defs if isinstance(defs, dict) else {}
    ids = _map_ids(api)
    nodes = {}
    for raw, node in api.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        vals = node.get("inputs") or {}
        specs = _input_specs(defs, ct)
        inputs, widgets, named, consumed = [], [], {}, set()
        if specs:
            for name, slot_type, opts, widget, raw_type in specs:
                val = vals.get(name)
                present = name in vals
                if _is_ref(val, api):
                    inputs.append({"name": name, "type": slot_type,
                                   "link": None})
                elif widget or present:
                    # a scalar value in the API means a widget input — even
                    # for types we don't classify as widgets (never drop it)
                    w = _widget_value(present, val, raw_type, opts)
                    widgets.append(w)
                    named[name] = w
                else:
                    inputs.append({"name": name, "type": slot_type,
                                   "link": None})
                consumed.add(name)
            extra = [(k, v) for k, v in vals.items() if k not in consumed]
        else:
            extra = list(vals.items())
        for name, val in extra:
            if _is_ref(val, api):
                inputs.append({"name": str(name), "type": "*", "link": None})
            else:
                widgets.append("" if val is None else val)
                named[str(name)] = widgets[-1]
        outs = []
        info = defs.get(ct) or {}
        out_types = info.get("output") or []
        out_names = info.get("output_name") or []
        for i, t in enumerate(out_types):
            nm = out_names[i] if i < len(out_names) else f"output_{i}"
            outs.append({"name": str(nm),
                         "type": t if isinstance(t, str) else "COMBO",
                         "links": [], "slot_index": i})
        gnode = {
            "id": ids[raw],
            "type": ct,
            "pos": [0, 0],
            "size": [315, max(80, 60 + 24 * len(widgets))],
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": inputs,
            "outputs": outs,
            "properties": {"Node name for S&R": ct} if ct else {},
            "widgets_values": widgets,
        }
        if named:
            # newer ComfyUI saves carry named widget values too; the
            # flattener (graph_to_api) prefers them, so keep both in sync
            gnode["widgets_values_named"] = named
        title = (node.get("_meta") or {}).get("title")
        if title:
            gnode["title"] = str(title)
        nodes[raw] = gnode

    # links — target_slot indexes the node's synthesized inputs[] array
    links = []
    for raw, node in api.items():
        gnode = nodes.get(raw)
        if not gnode:
            continue
        vals = node.get("inputs") or {}
        for slot, inp in enumerate(gnode["inputs"]):
            val = vals.get(inp["name"])
            if not _is_ref(val, api):
                continue
            src = nodes.get(str(val[0]))
            if not src:
                continue
            src_slot = int(val[1]) if isinstance(val[1], (int, float)) else 0
            lid = len(links) + 1
            links.append([lid, src["id"], src_slot, gnode["id"], slot,
                          inp["type"]])
            inp["link"] = lid
            while len(src["outputs"]) <= src_slot:
                src["outputs"].append(
                    {"name": f"output_{len(src['outputs'])}",
                     "type": inp["type"], "links": [],
                     "slot_index": len(src["outputs"])})
            src["outputs"][src_slot].setdefault("links", []).append(lid)

    # dependency order (sources first) + a readable auto-layout
    deps = {}
    for raw, node in api.items():
        if raw in nodes:
            deps[raw] = {str(v[0])
                         for v in (node.get("inputs") or {}).values()
                         if _is_ref(v, api) and str(v[0]) in nodes}
    order, seen = [], set()

    def visit(k, stack):
        if k in seen or k in stack:
            return
        stack.add(k)
        for dep in deps.get(k, ()):
            visit(dep, stack)
        stack.discard(k)
        if k not in seen:
            seen.add(k)
            order.append(k)

    for k in nodes:
        visit(k, set())

    depth = {}

    def level(k):
        if k not in depth:
            depth[k] = max([level(d) + 1 for d in deps.get(k, ())] or [0])
        return depth[k]

    rows = {}
    for i, k in enumerate(order):
        nodes[k]["order"] = i
        col = level(k)
        row = rows.get(col, 0)
        rows[col] = row + 1
        nodes[k]["pos"] = [40 + col * 380, 40 + row * 240]

    int_ids = [n["id"] for n in nodes.values() if isinstance(n["id"], int)]
    return {
        "id": str(uuid.uuid4()),
        "revision": 0,
        "last_node_id": max(int_ids) if int_ids else len(nodes),
        "last_link_id": len(links),
        "nodes": list(nodes.values()),
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }


def workflow_meta(configured_text, api, base):
    """The editable workflow to stash in extra_data.extra_pnginfo.workflow.

    ComfyUI's history action ("Open as workflow in a new tab") only accepts a
    *valid graph* in that field and silently opens nothing otherwise — an
    API-format blob there is worse than omitting it. So: a graph save the
    user configured is embedded verbatim (their own layout), anything else
    is synthesized into a graph from the API prompt that will actually run."""
    text = (configured_text or "").strip()
    if text:
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("nodes"), list):
            return obj
    return api_to_graph(api, fetch_object_info(base))


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


def _current_identity(cfg):
    """The cache key of the theme currently on screen (hold wins)."""
    try:
        import main as app
        st = app.load_state(cfg)
    except Exception:
        return ""
    hold = st.get("manual_hold")
    if isinstance(hold, dict) and hold.get("identity"):
        return str(hold["identity"])
    return str(st.get("last_cache_key") or "")


def drain_queue(cfg, log=print, max_jobs=1):
    """Run up to max_jobs queued video jobs (one by default — a single
    clip can take an hour). Applies the result when it belongs to the
    theme on screen. Returns [(cache_key, theme_title, project_path)].
    Never raises."""
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
        # don't inject an hour-long render into someone else's ComfyUI
        # work — wait until its queue is empty
        qq = requests.get(_base(cfg) + "/queue", timeout=5)
        if qq.status_code == 200:
            qj = qq.json() or {}
            if (qj.get("queue_running") or qj.get("queue_pending")):
                return []
    except Exception:
        log(f"  [livewall] {len(q)} video job(s) queued but ComfyUI is "
            "unreachable")
        return []
    done, remaining = [], []
    for n, job in enumerate(q):
        if n >= max_jobs:
            remaining.append(job)
            continue
        out = None
        try:
            out = generate(cfg, job["src"], job["dst"],
                           job.get("context"), log, defer=False)
        except Exception as e:
            log(f"  [livewall] queue job error: {e}")
        if not out:
            job["attempts"] = int(job.get("attempts") or 0) + 1
            if job["attempts"] >= 3:
                log("  [livewall] dropping video job after 3 failed "
                    "attempts")
            else:
                remaining.append(job)
            continue
        key = job.get("cache_key") or ""
        title = job.get("theme_title") or ""
        proj = install(cfg, job["dst"], key, title, job.get("hero") or "",
                       log)
        done.append((key, title, proj))
        if key and key == _current_identity(cfg):
            log("  [livewall] video ready for the theme on screen — "
                "applying it")
            apply_we(cfg, proj, _hero_device(cfg), log)
        elif key:
            log(f"  [livewall] video ready for '{title}' (kept cached; "
                "applies next time that theme is on screen)")
    with _queue_lock:
        tmp = _queue_file(cfg) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(remaining, f, indent=2)
        os.replace(tmp, _queue_file(cfg))
    return done


def start_drain(cfg, log=print):
    """Kick a background drain (non-blocking, single-flight) so the
    service loop stays responsive while a clip renders for an hour."""
    global _draining
    with _queue_lock:
        if _draining:
            return False
        _draining = True

    def work():
        global _draining
        try:
            drain_queue(cfg, log, max_jobs=1)
        except Exception as e:  # never kill the worker silently
            log(f"  [livewall] drain worker error: {e}")
        finally:
            with _queue_lock:
                _draining = False

    threading.Thread(target=work, daemon=True).start()
    return True


def _hero_device(cfg):
    """Best-effort hero display device for WE monitor mapping."""
    try:
        import theme as theme_mod
        mons = theme_mod.assign_roles(theme_mod.enumerate_monitors(),
                                      cfg.get("monitors", []),
                                      lambda m: None)
        return next((m["device"] for m in mons if m["role"] == "hero"), "")
    except Exception:
        return ""


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
    api, err = parse_workflow(lw.get("workflow") or "", log=log,
                              topaz_finish=lw.get("topaz_finish", True))
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
        # 4) submit + poll — stash a *valid graph* in extra_pnginfo.workflow
        #    so history's "Open as workflow in a new tab" can restore it
        #    (ComfyUI ignores API-format blobs there and opens nothing)
        payload = {"prompt": api}
        try:
            meta_obj = workflow_meta(lw.get("workflow"), api, base)
            if meta_obj:
                payload["extra_data"] = {"extra_pnginfo": {"workflow": meta_obj}}
        except Exception:
            pass
        r = requests.post(base + "/prompt", timeout=60, json=payload)
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
    finally:
        upscale._unload_comfy_models(cfg, base, log)


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


def publish_to_we(exe, project_path, log=print):
    """Copy the generated video into Wallpaper Engine's own myprojects
    folder (like WE's built-in video wallpapers) so the wallpaper survives
    cache wipes, and return the published .mp4 path (WE's -control accepts
    a video file directly). Returns None when there is no video."""
    import shutil
    import extras
    d = os.path.dirname(project_path)
    vid = next((os.path.join(d, n) for n in sorted(os.listdir(d))
                if n.lower().endswith(_VIDEO_EXTS)), None)
    if not vid:
        return None
    try:
        with open(project_path, encoding="utf-8") as f:
            proj = json.load(f)
    except (OSError, json.JSONDecodeError):
        proj = {}
    slug = extras._safe_title_name(proj.get("title") or "Steam Theme")
    dest = os.path.join(os.path.dirname(exe), "projects", "myprojects",
                        "steam-theme", slug)
    os.makedirs(dest, exist_ok=True)
    name = os.path.basename(vid)
    shutil.copyfile(vid, os.path.join(dest, name))
    with open(os.path.join(dest, "project.json"), "w",
              encoding="utf-8") as f:
        json.dump(dict(proj, file=name), f, indent=2)
    return os.path.join(dest, name)


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
    published = publish_to_we(exe, project_path, log)
    if not published:
        log("  [livewall] no video file next to the project — nothing to "
            "apply")
        return False
    idx = we_monitor(cfg, hero_device)
    if we_running():
        try:
            r = subprocess.run([exe, "-control", "openWallpaper",
                                "-file", published,
                                "-monitor", str(idx)],
                               capture_output=True, text=True, timeout=30)
            log(f"  [livewall] applied to Wallpaper Engine monitor {idx} "
                f"(exit {r.returncode}) -> {published}")
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
        sel[f"Monitor{idx}"] = {"file": published.replace("\\", "/")}
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

def _original_hero(path):
    """Video generation wants the ORIGINAL art, not an upscaled variant
    (huge uploads, and the workflow downsamples to its output size anyway).
    Maps hero_N_upscaled_<provider>-<hash>.<ext> back to hero_N.<ext>.
    Returns the input unchanged when it isn't an upscaled variant or no
    source file exists."""
    if not path:
        return path
    name = os.path.basename(path)
    if "_upscaled" not in name:
        return path
    stem = name.split("_upscaled")[0]
    folder = os.path.dirname(path)
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"):
        cand = os.path.join(folder, stem + ext)
        if os.path.isfile(cand):
            return cand
    return path


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
    art = None
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
    heroes = [hero] + [c for c in ((art or {}).get("hero_candidates") or [])
                       if isinstance(c, str) and c != hero]
    maybe_generate(cfg, heroes, ctx, cache_key,
                   (pal or {}).get("theme_name") or str(cache_key),
                   hero_device, log)


def artwork_from_cache(cache_dir):
    import artwork
    return artwork.art_from_cache(cache_dir)


def _hero_list(cfg, heroes):
    """Normalize to distinct, existing ORIGINAL hero paths. all_heroes
    set to false limits it to the active hero only. Returns [] when
    nothing usable."""
    if isinstance(heroes, str):
        heroes = [heroes]
    out, seen = [], set()
    for h in heroes or []:
        if not isinstance(h, str):
            continue
        h = _original_hero(h)
        if not h or not os.path.exists(h) or h in seen:
            continue
        seen.add(h)
        out.append(h)
    if not cfg.get("livewall", {}).get("all_heroes", True):
        out = out[:1]
    return out


def _hero_target(cfg, hero, cache_key):
    """(hash10, project_dir, video_path) for a hero's looping video."""
    with open(hero, "rb") as f:
        h = hashlib.sha1(f.read()).hexdigest()[:10]
    d = os.path.join(cfg.get("cache_dir") or "cache", str(cache_key),
                     "livewall", h)
    return h, d, os.path.join(d, "wallpaper.mp4")


def apply_for_hero(cfg, hero, cache_key, theme_title, hero_device, log=print):
    """Hand this hero's cached video to Wallpaper Engine (installing the
    project first when needed). True when a video existed."""
    hero = _original_hero(hero)
    if not hero or not os.path.exists(hero):
        return False
    try:
        _h, _d, dst = _hero_target(cfg, hero, cache_key)
    except OSError:
        return False
    if not os.path.exists(dst):
        return False
    proj = install(cfg, dst, cache_key, theme_title, hero, log)
    apply_we(cfg, proj, hero_device, log)
    return True


def sync_queue(cfg, cache_key, jobs):
    """Make the deferred queue match this game's hero set: drop jobs whose
    art no longer exists (a refetch changes hashes), add the missing ones.
    Returns how many were added."""
    wanted = {j["dst"]: j for j in jobs}
    with _queue_lock:
        try:
            with open(_queue_file(cfg), encoding="utf-8") as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            q = []
        if not isinstance(q, list):
            q = []
        existing = {j.get("dst") for j in q if isinstance(j, dict)}
        # stale entries for this game (old art) go away; other games stay
        q = [j for j in q if not (isinstance(j, dict)
                                  and j.get("cache_key") == cache_key
                                  and j.get("dst") not in wanted)]
        added = 0
        for dst, job in wanted.items():
            if dst in existing:
                continue
            q.append(job)
            added += 1
        tmp = _queue_file(cfg) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, _queue_file(cfg))
    return added


def maybe_generate(cfg, heroes, context, cache_key, theme_title,
                   hero_device, log=print, apply=True):
    """Theme-pipeline hook: make sure EVERY hero candidate has a looping
    video, put a ready one on the wallpaper, and queue the rest.

    Generation never happens here — a clip can take an hour, so the
    background drain worker renders queued heroes one at a time (gated on
    gaming / ComfyUI availability)."""
    out = {"heroes": 0, "cached": 0, "queued": 0, "applied": False}
    lw = cfg.get("livewall", {})
    if not (lw.get("enabled") and lw.get("provider") == "comfy"):
        return out
    hs = _hero_list(cfg, heroes)
    if not hs:
        return out
    out["heroes"] = len(hs)
    jobs, cached = [], 0
    for hero in hs:
        try:
            _h, _d, dst = _hero_target(cfg, hero, cache_key)
        except OSError:
            continue
        if os.path.exists(dst):
            cached += 1
            continue
        jobs.append({"src": hero, "dst": dst, "context": context or {},
                     "cache_key": cache_key, "theme_title": theme_title,
                     "hero": hero, "attempts": 0})
    out["cached"] = cached
    if jobs:
        added = sync_queue(cfg, cache_key, jobs)
        out["queued"] = added
        log(f"  [livewall] hero videos: {len(jobs)} to make "
            f"({cached} cached, {added} queued now) — original art, one at "
            "a time in the background")
    # a ready video for the active hero goes straight onto the wallpaper —
    # unless the caller is a cache pass (Regenerate All), which must never
    # change what's on screen
    if apply and apply_for_hero(cfg, hs[0], cache_key, theme_title,
                                hero_device, log):
        out["applied"] = True
        log(f"  [livewall] applied cached video for "
            f"{os.path.basename(hs[0])}")
    return out


def rotate_step(cfg, candidates, cache_key, theme_title, hero_device,
                log=print):
    """Rotation tick: switch the WE wallpaper to the next hero candidate
    that already has a video. Returns the hero path applied, or None."""
    hs = _hero_list(cfg, candidates)
    if len(hs) < 2:
        return None
    try:
        import main as app
        st = app.load_state(cfg)
    except Exception:
        return None
    rot = st.get("livewall_rotate")
    rot = rot if isinstance(rot, dict) else {}
    idx = int(rot.get("index") or 0) if str(
        rot.get("cache_key") or "") == str(cache_key) else 0
    for off in range(1, len(hs) + 1):
        j = (idx + off) % len(hs)
        if apply_for_hero(cfg, hs[j], cache_key, theme_title, hero_device,
                          log):
            st["livewall_rotate"] = {"cache_key": str(cache_key),
                                     "index": j, "at": time.time()}
            app.save_state(cfg, st)
            log(f"  [livewall] rotated to hero {j + 1}/{len(hs)}")
            return hs[j]
    return None