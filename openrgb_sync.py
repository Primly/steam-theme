"""OpenRGB lighting sync — the Linux counterpart to extras.apply_signalrgb.

SignalRGB doesn't exist for Linux; OpenRGB (https://openrgb.org) is the
standard cross-vendor RGB stack there. It exposes an SDK server (TCP 6742)
that lets clients push per-LED colors to devices in 'Direct' mode.

We use the 'openrgb' pip package (a maintained SDK client) rather than
hand-rolling the binary protocol. Everything here is best-effort: failures
are logged, never fatal.

Setup (Bazzite):  ujust openrgb install   then run:  openrgb --server
"""

import re

_HEX6 = re.compile(r"#[0-9a-fA-F]{6}")


def _hex_to_rgbcolor(hexcolor, rgbcolor_cls):
    """'#66C0F4' -> RGBColor(102, 192, 244); None for anything else."""
    if not _HEX6.fullmatch(str(hexcolor)):
        return None
    h = str(hexcolor).lstrip("#")
    return rgbcolor_cls(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _direct_mode_index(device):
    """Index of the device's per-LED mode ('Direct'/'Static'), or None."""
    for i, mode in enumerate(getattr(device, "modes", []) or []):
        name = (getattr(mode, "name", "") or "").lower()
        if "direct" in name or "static" in name:
            return i
    return None


def apply_openrgb(cfg, palette, log=print):
    ocfg = cfg.get("openrgb", {})
    if not ocfg.get("enabled"):
        return
    try:
        from openrgb import OpenRGBClient
        from openrgb.utils import RGBColor
    except ImportError:
        log("  [openrgb] python package 'openrgb' not installed — "
            "pip install openrgb (it is in requirements.txt for Linux)")
        return

    host = ocfg.get("host", "127.0.0.1")
    port = int(ocfg.get("port", 6742))
    try:
        client = OpenRGBClient(host, port, name="Steam Theme")
    except Exception as e:
        log(f"  [openrgb] SDK server not reachable at {host}:{port} ({e}); "
            "start it with: openrgb --server")
        return

    # build the LED color cycle: accent first, then palette colors
    raw = [palette.get("accent")] + list(palette.get("colors") or [])
    colors = [c for c in (_hex_to_rgb_color_safe(h, RGBColor) for h in raw)
              if c is not None]
    if not colors:
        log("  [openrgb] palette has no usable colors")
        return

    themed, skipped = 0, 0
    for device in list(getattr(client, "devices", []) or []):
        try:
            idx = _direct_mode_index(device)
            if idx is None:
                skipped += 1
                continue
            device.set_mode(idx)
            n_leds = len(getattr(device, "leds", []) or [])
            if not n_leds:
                skipped += 1
                continue
            device.set_colors([colors[i % len(colors)] for i in range(n_leds)])
            themed += 1
        except Exception as e:
            name = getattr(device, "name", "?")
            log(f"  [openrgb] device {name!r} failed: {e}")
            skipped += 1
    try:
        client.disconnect()
    except Exception:
        pass
    log(f"  [openrgb] palette pushed to {themed} device(s)"
        + (f", {skipped} skipped (no Direct mode)" if skipped else ""))


def _hex_to_rgb_color_safe(hexcolor, rgbcolor_cls):
    try:
        return _hex_to_rgbcolor(hexcolor, rgbcolor_cls)
    except Exception:
        return None
