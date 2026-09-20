"""Stage 3: palette extraction (Aether-style, Python port).

- 16-color median-cut palette via PIL (Material / Muted / Colorful modes)
- WCAG contrast-checked accent against the chosen background (AA grading)
- dark/light decision from weighted palette luminance
- optional local-VLM / OpenRouter call for mood + theme naming
"""

import base64
import colorsys
import json
import re

from PIL import Image

NEAR_BLACK = (16, 16, 20)
NEAR_WHITE = (245, 245, 245)


def _hex(c):
    return "#{:02X}{:02X}{:02X}".format(*c)


def _rel_luminance(c):
    def chan(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    l1, l2 = sorted((_rel_luminance(a), _rel_luminance(b)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _adjust_mode(c, mode):
    r, g, b = (x / 255.0 for x in c)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if mode == "muted":
        s *= 0.55
    elif mode == "material":
        s = min(max(s, 0.35), 0.70)
        l = min(max(l, 0.30), 0.70)
    elif mode == "colorful":
        s = min(1.0, s * 1.35 + 0.05)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (round(r * 255), round(g * 255), round(b * 255))


def extract_palette(image_path, mode="colorful", n=16):
    """Median-cut palette. Returns (colors, weights) as RGB tuples + dominance."""
    with Image.open(image_path) as im:
        small = im.convert("RGB").resize((160, 160))
    q = small.quantize(colors=n, method=Image.MEDIANCUT)
    raw = q.getpalette()[: n * 3]
    counts = sorted(q.getcolors(256) or [], reverse=True)  # [(count, idx)]
    colors, weights = [], []
    for count, idx in counts:
        c = tuple(raw[idx * 3: idx * 3 + 3])
        colors.append(_adjust_mode(c, mode))
        weights.append(count)
    while len(colors) < n:  # pad if image had < n distinct colors
        colors.append(colors[-1] if colors else NEAR_BLACK)
        weights.append(1)
    return colors, weights


def decide_mode(colors, weights):
    total = sum(weights)
    lum = sum(_rel_luminance(c) * w for c, w in zip(colors, weights)) / total
    return "dark" if lum < 0.45 else "light"


def pick_accent(colors, weights, background):
    """Pick a chromatic color, then nudge lightness until it passes WCAG AA (4.5:1)."""
    scored = []
    for c, w in zip(colors, weights):
        r, g, b = (x / 255.0 for x in c)
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        scored.append((w * (0.25 + s), h, l, s, c))
    scored.sort(reverse=True)

    dark_bg = _rel_luminance(background) < 0.2
    for _, h, l, s, c in scored[:6]:
        if s < 0.08:  # skip greys until we run out of chromatic options
            continue
        step = 0.06 if dark_bg else -0.06
        for _ in range(12):
            trial = colorsys.hls_to_rgb(h, min(max(l, 0.0), 1.0), s)
            trial = tuple(round(x * 255) for x in trial)
            if contrast(trial, background) >= 4.5:
                return trial, "AA"
            l += step
        # accept best-effort if AA unreachable
        trial = colorsys.hls_to_rgb(h, min(max(l, 0.0), 1.0), s)
        trial = tuple(round(x * 255) for x in trial)
        if contrast(trial, background) >= 3.0:
            return trial, "AA-large"
    return (colors[0] if colors else (127, 127, 127)), "ungraded"


def build_palette(image_path, mode="colorful", force_dark=False):
    colors, weights = extract_palette(image_path, mode)
    appearance = "dark" if force_dark else decide_mode(colors, weights)
    background = NEAR_BLACK if appearance == "dark" else NEAR_WHITE
    accent, grade = pick_accent(colors, weights, background)
    return {
        "mode": mode,
        "appearance": appearance,          # "dark" | "light"
        "colors": [_hex(c) for c in colors],
        "background": _hex(background),
        "accent": _hex(accent),
        "accent_contrast": round(contrast(accent, background), 2),
        "accent_grade": grade,
    }


# ---------------------------------------------------------------- VLM naming

def _chat_completion(base_url, api_key, model, messages, timeout=60):
    import requests
    r = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "temperature": 0.4},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def ai_theme_naming(cfg, image_path, palette, game_name, log=print):
    """Ask a local VLM (OpenAI-compatible, e.g. LM Studio) or OpenRouter for a
    mood classification + theme name. Returns dict or None. Never raises."""
    src = None
    if cfg.get("vlm", {}).get("enabled"):
        v = cfg["vlm"]
        src = (v["base_url"], v.get("api_key", "lm-studio"), v["model"], True)
    elif cfg.get("openrouter", {}).get("enabled") and cfg["openrouter"].get("api_key"):
        o = cfg["openrouter"]
        src = ("https://openrouter.ai/api/v1", o["api_key"], o["model"], True)
    if not src:
        return None
    base_url, key, model, _ = src

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    prompt = (
        f"This is key art from the game '{game_name}'. Its dominant colors are "
        f"{', '.join(palette['colors'][:8])}. Respond with ONLY a JSON object: "
        '{"theme_name": "<short evocative name>", "mood": "<e.g. dark fantasy, '
        'neon cyberpunk, cozy pixel>", "appearance": "dark"|"light", '
        '"palette_mode": "material"|"muted"|"colorful"}'
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ],
    }]
    try:
        text = _chat_completion(base_url, key, model, messages)
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        log(f"  [vlm] naming failed (continuing without it): {e}")
        return None
