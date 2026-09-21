"""SignalRGB Lightscript effect templates, skinned with the theme palette.

Each style is a self-contained HTML canvas effect (the documented Lightscript
format: meta properties in <head>, canvas in <body>, animation in <script>).
The palette colors are baked in as color-picker defaults (color1/color2/
color3), so the user can still tweak them in the SignalRGB UI between games.

Keypress reactivity: SignalRGB calls a lightscript's global
`onCanvasTapped(x, y)` (coordinates in the 320x200 canvas space, mapped from
the physical key position) whenever a key is pressed on a device with per-key
LED positions. This is the same hook the first-party "WhirlwindFX"/"SignalRGB"
publisher effects use (e.g. Neon Sunset, Dark Matter, Ripples). Every style
below layers palette-colored tap rings (a radial flash for Solid) on top of
its ambient animation, gated by a "Keypress Effects" toggle. Keytap input is
a SignalRGB Pro feature and only fires on key-mapped devices (keyboards);
other devices simply keep playing the ambient part.
"""

STYLES = {
    "gradient": "Gradient Sweep (palette gradient scrolling in a chosen direction)",
    "pulse":    "Pulse (accent glow breathing from the center)",
    "ripple":   "Ripple (ambient expanding rings + keypress ripples)",
    "rain":     "Rain (palette streaks falling on near-black)",
    "comet":    "Comet (bright accent bar sweeping with a palette trail)",
    "solid":    "Solid (flat accent, optional breathing)",
}

_HEAD = """<head>
  <title>{title}</title>
  <meta description="Auto-generated from the current Steam game theme by SteamWallpaper. Colors are regenerated per game; tweak freely in between."/>
  <meta publisher="SteamWallpaper"/>
  <meta property="color1" label="Accent" type="color" min="0" max="360" default="{c1}"/>
  <meta property="color2" label="Palette 2" type="color" min="0" max="360" default="{c2}"/>
  <meta property="color3" label="Palette 3" type="color" min="0" max="360" default="{c3}"/>
  <meta property="speed" label="Speed" type="number" min="1" max="10" default="4"/>
  <meta property="tapEffects" label="Keypress Effects" type="boolean" default="1"/>
{extra_meta}</head>
<body style="margin: 0; padding: 0;">
  <canvas id="exCanvas" width="320" height="200"></canvas>
</body>
"""

_BOOT = """
<script>
  var c = document.getElementById("exCanvas");
  var ctx = c.getContext("2d");
  var W = 320, H = 200;

  function hexToRgb(hex) {
    var m = /^#?([a-f\\d]{2})([a-f\\d]{2})([a-f\\d]{2})$/i.exec(hex);
    return m ? [parseInt(m[1],16), parseInt(m[2],16), parseInt(m[3],16)] : [0,0,0];
  }
  function rgbStr(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + (a==null?1:a) + ")"; }
  // SignalRGB injects meta-property globals (color1, speed, ...) AFTER the
  // page script runs, so reading them at top level aborts the script with a
  // ReferenceError and every engine callback then fails on the unassigned
  // vars. Everything property-derived is built lazily by ensureInit(), first
  // called from the rAF loop / engine callbacks.
  var C1 = null, C2 = null, C3 = null, PAL = null;
  var ready = false;
  function ensureInit() {
    if (ready) return true;
    if (typeof color1 === "undefined") return false;
    C1 = hexToRgb(color1); C2 = hexToRgb(color2); C3 = hexToRgb(color3);
    PAL = [C1, C2, C3];
    ready = true;
    return true;
  }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function palColor(t) {  // t in [0,1), cycles through palette
    t = ((t % 1) + 1) % 1;
    var seg = t * PAL.length, i = Math.floor(seg) % PAL.length, j = (i + 1) % PAL.length;
    var f = seg - Math.floor(seg);
    return [lerp(PAL[i][0], PAL[j][0], f), lerp(PAL[i][1], PAL[j][1], f), lerp(PAL[i][2], PAL[j][2], f)];
  }
  function oncolor1Changed() { if (!ensureInit()) return; C1 = hexToRgb(color1); PAL[0] = C1; }
  function oncolor2Changed() { if (!ensureInit()) return; C2 = hexToRgb(color2); PAL[1] = C2; }
  function oncolor3Changed() { if (!ensureInit()) return; C3 = hexToRgb(color3); PAL[2] = C3; }

  // --- keypress layer -------------------------------------------------------
  // SignalRGB calls onCanvasTapped(x, y) in 320x200 canvas coordinates when a
  // key is pressed on a key-mapped device (Pro feature). Each style draws its
  // ambient frame first, then calls drawTaps() so taps always sit on top.
  var taps = [];
  function onCanvasTapped(x, y) {
    if (typeof tapEffects !== "undefined" && !tapEffects) return;
    if (!ensureInit()) return;
    taps.push({x: x, y: y, r: 2, life: 1.0});
  }
  function drawTaps() {  // double ring, like the stock ripple keytap effects
    if (!taps) return;
    for (var i = taps.length - 1; i >= 0; i--) {
      var tp = taps[i];
      tp.r += 1.5 + speed / 3;
      tp.life -= 0.02;
      if (tp.life <= 0) { taps.splice(i, 1); continue; }
      ctx.beginPath();
      ctx.strokeStyle = rgbStr(C1, tp.life);
      ctx.lineWidth = 7;
      ctx.arc(tp.x, tp.y, tp.r, 0, 2 * Math.PI);
      ctx.stroke();
      ctx.beginPath();
      ctx.strokeStyle = rgbStr(C2, tp.life * 0.8);
      ctx.lineWidth = 4;
      ctx.arc(tp.x, tp.y, tp.r * 0.7, 0, 2 * Math.PI);
      ctx.stroke();
    }
  }
"""


def _page(title, c1, c2, c3, extra_meta, body_js):
    return (_HEAD.format(title=title, c1=c1, c2=c2, c3=c3, extra_meta=extra_meta)
            + _BOOT + body_js + "\n</script>\n")


def _solid(title, c1, c2, c3):
    meta = '  <meta property="breathe" label="Breathing" type="boolean" default="0"/>'
    js = """
  // a radial flash reads better than rings on a flat accent base
  drawTaps = function () {
    for (var i = taps.length - 1; i >= 0; i--) {
      var tp = taps[i];
      tp.r += 2 + speed / 2;
      tp.life -= 0.04;
      if (tp.life <= 0) { taps.splice(i, 1); continue; }
      var g = ctx.createRadialGradient(tp.x, tp.y, 1, tp.x, tp.y, tp.r);
      g.addColorStop(0, rgbStr(C2, tp.life * 0.9));
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(tp.x, tp.y, tp.r, 0, 2 * Math.PI);
      ctx.fill();
    }
  };
  var t = 0;
  function update() {
    if (!ensureInit()) { window.requestAnimationFrame(update); return; }
    var a = 1;
    if (breathe) { t += speed / 200; a = Math.sin(t) * 0.35 + 0.65; }
    ctx.fillStyle = rgbStr(C1, a);
    ctx.fillRect(0, 0, W, H);
    drawTaps();
    window.requestAnimationFrame(update);
  }
  window.requestAnimationFrame(update);
"""
    return _page(title, c1, c2, c3, meta, js)


def _gradient(title, c1, c2, c3):
    meta = ('  <meta property="direction" label="Direction" type="list" '
            'values="Left to Right,Right to Left,Top to Bottom,Bottom to Top" '
            'default="Left to Right"/>')
    js = """
  var phase = 0;
  function update() {
    if (!ensureInit()) { window.requestAnimationFrame(update); return; }
    phase += speed / 4000;
    var horizontal = (direction.indexOf("Left") >= 0 || direction.indexOf("Right") >= 0);
    var reverse = (direction.indexOf("Right") >= 0 || direction.indexOf("Bottom") >= 0);
    var n = horizontal ? W : H, i, col;
    for (i = 0; i < n; i++) {
      var t = i / n;
      if (reverse) t = 1 - t;
      col = palColor(t + phase);
      ctx.fillStyle = rgbStr(col);
      if (horizontal) ctx.fillRect(i, 0, 1, H); else ctx.fillRect(0, i, W, 1);
    }
    drawTaps();
    window.requestAnimationFrame(update);
  }
  window.requestAnimationFrame(update);
"""
    return _page(title, c1, c2, c3, meta, js)


def _pulse(title, c1, c2, c3):
    js = """
  var t = 0;
  function update() {
    if (!ensureInit()) { window.requestAnimationFrame(update); return; }
    t += speed / 150;
    var p = Math.sin(t) * 0.5 + 0.5;            // 0..1
    ctx.fillStyle = "black";
    ctx.fillRect(0, 0, W, H);
    var g = ctx.createRadialGradient(W/2, H/2, 10, W/2, H/2, 60 + p * 180);
    g.addColorStop(0, rgbStr(C1, 0.9));
    g.addColorStop(0.5, rgbStr(C2, 0.45 * p));
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);
    drawTaps();
    window.requestAnimationFrame(update);
  }
  window.requestAnimationFrame(update);
"""
    return _page(title, c1, c2, c3, "", js)


def _ripple(title, c1, c2, c3):
    meta = ('  <meta property="ambient" label="Ambient ripples" type="boolean" default="1"/>\n'
            '  <meta property="base" label="Base color" type="color" min="0" max="360" default="#0A0A0E"/>')
    js = """
  var ripples = [], frame = 0;
  function update() {
    if (!ensureInit()) { window.requestAnimationFrame(update); return; }
    frame++;
    ctx.fillStyle = (typeof base !== "undefined") ? base : "#0A0A0E";
    ctx.fillRect(0, 0, W, H);
    if (ambient && frame % Math.max(10, 60 - speed * 5) === 0) {
      ripples.push({x: Math.random()*W, y: Math.random()*H, r: 4, life: 1, col: PAL[frame % PAL.length]});
    }
    for (var i = ripples.length - 1; i >= 0; i--) {
      var rp = ripples[i];
      rp.r += 1 + speed / 3; rp.life -= 0.012;
      if (rp.life <= 0) { ripples.splice(i, 1); continue; }
      ctx.beginPath();
      ctx.strokeStyle = rgbStr(rp.col, rp.life);
      ctx.lineWidth = 6;
      ctx.arc(rp.x, rp.y, rp.r, 0, 2 * Math.PI);
      ctx.stroke();
    }
    drawTaps();
    window.requestAnimationFrame(update);
  }
  window.requestAnimationFrame(update);
"""
    return _page(title, c1, c2, c3, meta, js)


def _rain(title, c1, c2, c3):
    js = """
  var drops = null;  // built on first ready frame: needs PAL, which needs
                     // the engine-injected property globals
  function update() {
    if (!ensureInit()) { window.requestAnimationFrame(update); return; }
    if (!drops) {
      drops = [];
      for (var k = 0; k < 70; k++) {
        drops.push({x: Math.random()*W, y: Math.random()*H, v: 1 + Math.random()*3,
                    len: 8 + Math.random()*18, col: PAL[k % PAL.length]});
      }
    }
    ctx.fillStyle = "rgba(5,5,8,0.25)";   // trail fade
    ctx.fillRect(0, 0, W, H);
    for (var i = 0; i < drops.length; i++) {
      var d = drops[i];
      d.y += d.v * (0.5 + speed / 5);
      if (d.y - d.len > H) { d.y = -d.len; d.x = Math.random()*W; }
      ctx.fillStyle = rgbStr(d.col, 0.9);
      ctx.fillRect(d.x, d.y - d.len, 2, d.len);
    }
    drawTaps();
    window.requestAnimationFrame(update);
  }
  window.requestAnimationFrame(update);
"""
    return _page(title, c1, c2, c3, "", js)


def _comet(title, c1, c2, c3):
    js = """
  var x = 0;
  function update() {
    if (!ensureInit()) { window.requestAnimationFrame(update); return; }
    ctx.fillStyle = "rgba(5,5,8,0.18)";   // trail fade
    ctx.fillRect(0, 0, W, H);
    x += 1 + speed / 2;
    if (x > W + 60) x = -60;
    var g = ctx.createLinearGradient(x - 60, 0, x, 0);
    g.addColorStop(0, "rgba(0,0,0,0)");
    g.addColorStop(0.7, rgbStr(C2, 0.5));
    g.addColorStop(1, rgbStr(C1, 1));
    ctx.fillStyle = g;
    ctx.fillRect(x - 60, 0, 60, H);
    drawTaps();
    window.requestAnimationFrame(update);
  }
  window.requestAnimationFrame(update);
"""
    return _page(title, c1, c2, c3, "", js)


_RENDERERS = {"solid": _solid, "gradient": _gradient, "pulse": _pulse,
              "ripple": _ripple, "rain": _rain, "comet": _comet}


def render_effect(style, title, palette):
    """Render a Lightscript HTML file for `style` skinned with `palette`."""
    colors = list(dict.fromkeys([palette["accent"]] + palette.get("colors", [])))[:3]
    while len(colors) < 3:
        colors.append(palette["accent"])
    fn = _RENDERERS.get(style, _gradient)
    return fn(title, colors[0], colors[1], colors[2])
