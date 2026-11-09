// Video store: a faint tracking line drifting down through the header band, with a little jitter (docs/themes.md,
// "Effects"). One canvas as high as the header band, drawn at 3 frames a second. The line fades with the band's height and
// shows only through the see-through parts of the header (the shop's fascia and the floor the aisle signs hang over),
// never over the page's content. The band's height is the skin's
// (--vs-band), the colour is the theme's --accent; both are read in each frame.

const FPS = 3;
const CYCLE_S = 14; // seconds for the line to cross the band
const ALPHA = 0.3;

const px = (value, fallback) => {
  const n = parseFloat(value);
  return Number.isFinite(n) && n > 0 && n < 400 ? n : fallback;
};

export default {
  start(layer, ctx) {
    const canvas = document.createElement("canvas");
    canvas.className = "vs-track";
    layer.append(canvas);
    const g = canvas.getContext("2d");
    let width = 0;
    let band = 0;
    let scale = 1;
    let phase = 0;
    let tick = 0;

    const off = ctx.onFrame((now, dt) => {
      const t = ctx.tokens();
      const nextBand = px(t["--vs-band"], 100);
      if (nextBand !== band || layer.clientWidth !== width) {
        band = nextBand;
        width = layer.clientWidth;
        scale = Math.min(2, window.devicePixelRatio || 1);
        canvas.width = Math.round(width * scale);
        canvas.height = Math.round(band * scale);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${band}px`;
      }
      phase = (phase + Math.min(dt, 1000) / 1000 / CYCLE_S) % 1;
      tick++;
      g.setTransform(scale, 0, 0, scale, 0, 0);
      g.clearRect(0, 0, width, band);
      g.fillStyle = t["--accent"];
      const y = Math.round(phase * (band - 6));
      g.globalAlpha = ALPHA;
      g.fillRect(0, y, width, 3);
      g.globalAlpha = ALPHA * 0.6;
      g.fillRect(0, y + 4, width, 1);
      // a few short noisy streaks that shift from frame to frame
      for (let i = 0; i < 5; i++) {
        const r = Math.sin((tick + 1) * (i + 1.7) * 12.9898) * 43758.5453;
        const f = r - Math.floor(r);
        const w = 20 + f * 90;
        g.fillRect(f * (width - w), Math.min(band - 2, y + 6 + i * 2), w, 1);
      }
      g.globalAlpha = 1;
    }, FPS);

    return () => {
      off();
      canvas.remove();
    };
  },
};
