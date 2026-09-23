"""Shared, platform-agnostic wallpaper compositing (PIL only — no OS imports).

Used by both theme.py (Windows: one span image across all monitors) and
linux_theme.py (KDE: one exact-sized image per screen).
"""

import glob
import os

from PIL import Image, ImageEnhance, ImageFilter


# ------------------------------------------------------------ monitor roles

def assign_roles(monitors, mapping, log=print):
    """Attach a 'role' (hero/logo/icon) to each monitor.

    Config matches win by substring against the monitor description;
    unmatched monitors default by size: largest=hero, second=logo, third=icon.
    """
    defaults = ["hero", "logo", "icon"]
    by_area = sorted(monitors, key=lambda m: (m["rect"][2] - m["rect"][0])
                     * (m["rect"][3] - m["rect"][1]), reverse=True)
    for i, m in enumerate(by_area):
        m["role"] = defaults[i] if i < len(defaults) else "hero"
    for entry in mapping:
        needle = entry["match"].lower()
        for m in monitors:
            if needle in m["desc"].lower() or needle in m["device"].lower():
                m["role"] = entry["role"]
    for m in monitors:
        log(f"  [mon] {m['device']} {m['desc']!r} "
            f"{m['rect'][2]-m['rect'][0]}x{m['rect'][3]-m['rect'][1]} -> {m['role']}")
    return monitors


# ------------------------------------------------------------- compositing

def _cover(img, w, h):
    """Resize+center-crop so img exactly fills w x h."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((max(1, round(iw * scale)), max(1, round(ih * scale))),
                     Image.LANCZOS)
    x = (img.size[0] - w) // 2
    y = (img.size[1] - h) // 2
    return img.crop((x, y, x + w, y + h))


def _fit(img, w, h, max_upscale=3.0):
    """Resize to fit inside w x h, preserving aspect (limited upscaling)."""
    iw, ih = img.size
    scale = min(min(w / iw, h / ih), max_upscale)
    return img.resize((max(1, round(iw * scale)), max(1, round(ih * scale))),
                     Image.LANCZOS)


def _has_transparency(img):
    if img.mode in ("RGBA", "LA"):
        lo, _hi = img.getchannel("A").getextrema()
        return lo < 250
    return img.mode == "P" and "transparency" in img.info


def _centered_on_backdrop(img, hero, w, h):
    """For logos/icons: darkened blurred hero backdrop + art centered."""
    backdrop = _cover(hero.copy(), w, h).filter(ImageFilter.GaussianBlur(24))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.4)
    fg = _fit(img.convert("RGBA"), round(w * 0.8), round(h * 0.7))
    x, y = (w - fg.size[0]) // 2, (h - fg.size[1]) // 2
    backdrop.paste(fg, (x, y), fg)
    return backdrop.convert("RGB")


def compose_monitor_tile(monitor, art, hero, w, h, log=print):
    """One monitor's wallpaper tile at exactly w x h.

    Scene art covers the monitor; logos/icons (transparent, oddly
    proportioned, or small) get centered on a blurred hero backdrop.
    """
    src = art.get(monitor["role"]) or hero
    img = Image.open(src)
    iw, ih = img.size
    covers = (iw >= w * 0.6 and ih >= h * 0.6
              and abs((iw / ih) / (w / h) - 1) < 0.35)
    centered = (monitor["role"] in ("logo", "icon") and src != hero
                and (_has_transparency(img) or not covers))
    if centered:
        log(f"  [wall] {monitor['role']} art is transparent/small -> centered treatment")
        return _centered_on_backdrop(img, Image.open(hero).convert("RGB"), w, h)
    return _cover(img.convert("RGB"), w, h)


def _prune_old_composites(cache_dir, keep_paths):
    """Remove previous composite files; the OS keeps its own rendered copy."""
    keep = {os.path.abspath(p) for p in keep_paths}
    for old in (glob.glob(os.path.join(cache_dir, "wallpaper_*.jpg"))
                + glob.glob(os.path.join(cache_dir, "wallpaper_span.jpg"))):
        if os.path.abspath(old) not in keep:
            try:
                os.remove(old)
            except OSError:
                pass


def compose_wallpaper(monitors, art, out_path, log=print):
    """Windows strategy: ONE span-style image over the bounding box of all
    monitors; Windows' 'Span' wallpaper style maps it 1:1."""
    min_x = min(m["rect"][0] for m in monitors)
    min_y = min(m["rect"][1] for m in monitors)
    max_x = max(m["rect"][2] for m in monitors)
    max_y = max(m["rect"][3] for m in monitors)
    W, H = max_x - min_x, max_y - min_y

    hero = art.get("hero") or next(iter(art.values()))
    canvas = _cover(Image.open(hero).convert("RGB"), W, H)  # fills any gaps

    for m in monitors:
        l, t, r, b = m["rect"]
        tile = compose_monitor_tile(m, art, hero, r - l, b - t, log)
        canvas.paste(tile, (l - min_x, t - min_y))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path, "JPEG", quality=92)
    _prune_old_composites(os.path.dirname(os.path.abspath(out_path)),
                          [out_path])
    log(f"  [wall] composed {W}x{H} span wallpaper -> {out_path}")
    return out_path


# ----------------------------------------------------------- text sanitation

def _ini_safe(text, limit=128):
    """Strip control characters from a value headed for an INI-style file.
    A theme name containing a newline could otherwise inject extra keys
    (e.g. a screensaver path in .theme, or extra groups in .colorscheme).
    Theme names can come from a VLM, so treat them as untrusted."""
    return "".join(ch for ch in str(text) if 32 <= ord(ch) < 127)[:limit]
