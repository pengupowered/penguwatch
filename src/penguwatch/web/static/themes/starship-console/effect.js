// Starship console: a faint sweep of light along the header band, left to right (docs/themes.md, "Effects").
//
// One canvas in the effect layer, only as high as the header band. Each frame draws a soft bar (a few dozen thin slices of
// falling alpha) at a position that advances slowly, then fades the lower part away so nothing is drawn behind the page's
// content: only behind the see-through header. A few frames a second are enough for a slow sweep. The band's height is
// the skin's (--sc-band: the header's height changes with the window) and the colours are the theme's tokens, both read in
// each frame (a resize is noticed there too). The sweep shows only through the header's see-through black, beside the
// skin's opaque corner block and top bar. While this effect runs (#fx-layer[data-state="running"]) the skin also lets
// two blocks at the foot of the rail brighten in turn (skin.css, an opacity animation under that gate).

const FPS = 3;
const CYCLE_S = 16; // seconds for one pass across the window
const WIDTH = 260; // px, the bar's width
const SLICES = 26;
const ALPHA = 0.22;

const px = (value, fallback) => {
  const n = parseFloat(value);
  return Number.isFinite(n) && n > 0 && n < 400 ? n : fallback;
};

export default {
  start(layer, ctx) {
    const canvas = document.createElement("canvas");
    canvas.className = "sc-sweep";
    layer.append(canvas);
    const g = canvas.getContext("2d");
    let width = 0;
    let band = 0;
    let scale = 1;
    let phase = 0; // 0..1 across the window and the bar's own width

    const size = () => {
      width = layer.clientWidth;
      scale = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(band * scale);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${band}px`;
    };

    const off = ctx.onFrame((now, dt) => {
      const t = ctx.tokens();
      const nextBand = px(t["--sc-band"], 100);
      if (nextBand !== band || layer.clientWidth !== width) {
        band = nextBand;
        size();
      }
      phase = (phase + Math.min(dt, 1000) / 1000 / CYCLE_S) % 1;
      const x0 = -WIDTH + phase * (width + WIDTH);
      const slice = WIDTH / SLICES;
      g.setTransform(scale, 0, 0, scale, 0, 0);
      g.clearRect(0, 0, width, band);
      g.fillStyle = t["--s2"];
      for (let i = 0; i < SLICES; i++) {
        g.globalAlpha = ALPHA * Math.sin(((i + 0.5) / SLICES) * Math.PI);
        g.fillRect(Math.round(x0 + i * slice), 0, Math.ceil(slice), band);
      }
      g.globalAlpha = 1;
      // fade the lower part out (destination-out with a gradient to an opaque token colour: only the alpha counts)
      const grad = g.createLinearGradient(0, band * 0.35, 0, band);
      grad.addColorStop(0, "transparent");
      grad.addColorStop(1, t["--bg"]);
      g.globalCompositeOperation = "destination-out";
      g.fillStyle = grad;
      g.fillRect(0, band * 0.35, width, band * 0.65);
      g.globalCompositeOperation = "source-over";
    }, FPS);

    return () => {
      off();
      canvas.remove();
    };
  },
};
