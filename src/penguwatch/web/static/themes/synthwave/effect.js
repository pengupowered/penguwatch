// Synthwave: a perspective grid floor at the bottom of the window, its rows drifting slowly toward you (docs/themes.md,
// "Effects").
//
// Two canvases in the effect layer, only as high as the floor (the skin's --sw-floor; the skin places them at the bottom
// of the window, above the phone's tab bar): the still part (the rays running from the vanishing point on the horizon,
// fading into the haze there), drawn when the size or the colours change, and the rows, drawn at FPS frames a second in
// the engine's frame loop. A row sits at depth / z px below the horizon for its depth z; near the horizon a row hardly
// moves, so a few frames a second are enough. A row's strength grows with its distance from the horizon, so the grid is
// strongest at the window's bottom edge and needs no fade pass. The layer is behind the page: the floor shows between and
// below the cards, never over them. The geometry and the colours are read in each frame (a resize is noticed there too).

const STEP = 0.5; // depth between two rows
const CYCLE_S = 9; // seconds for a row to drift to where the next one was
const FPS = 4;
const RAYS = 14; // rays on each side of the vanishing point
const ROW_ALPHA = 0.7; // a row's strength at the bottom edge
const RAY_ALPHA = 0.42;
const FAINTEST = 0.03; // rows fainter than this, near the horizon, are not drawn

const px = (value, fallback) => {
  const n = parseFloat(value);
  return Number.isFinite(n) && n > 0 && n < 1000 ? n : fallback;
};

export default {
  start(layer, ctx) {
    const still = document.createElement("canvas");
    const rows = document.createElement("canvas");
    still.className = "sw-rays";
    rows.className = "sw-rows";
    layer.append(still, rows);
    const gs = still.getContext("2d");
    const gr = rows.getContext("2d");
    let width = 0;
    let band = 0;
    let scale = 1;
    let drawn = ""; // the colours and geometry the still canvas was drawn with
    let phase = 0;

    const size = () => {
      width = layer.clientWidth;
      scale = Math.min(2, window.devicePixelRatio || 1);
      for (const c of [still, rows]) {
        c.width = Math.round(width * scale);
        c.height = Math.round(band * scale);
        c.style.width = `${width}px`;
        c.style.height = `${band}px`;
      }
      drawn = "";
    };

    // the rays, then the haze over the horizon: erased fully at the top, untouched from 60 % of the floor down
    // (destination-out with a gradient from an opaque token colour to "transparent": only the stops' alpha counts)
    const drawStill = (cyan, bg) => {
      gs.setTransform(scale, 0, 0, scale, 0, 0);
      gs.clearRect(0, 0, width, band);
      gs.strokeStyle = cyan;
      gs.lineWidth = 1;
      gs.globalAlpha = RAY_ALPHA;
      const mid = width / 2;
      gs.beginPath();
      for (let i = -RAYS; i <= RAYS; i++) {
        gs.moveTo(mid, 0);
        gs.lineTo(mid + i * (width / RAYS) * 1.3, band);
      }
      gs.stroke();
      gs.globalAlpha = 1;
      const haze = gs.createLinearGradient(0, 0, 0, band * 0.6);
      haze.addColorStop(0, bg);
      haze.addColorStop(1, "transparent");
      gs.globalCompositeOperation = "destination-out";
      gs.fillStyle = haze;
      gs.fillRect(0, 0, width, band * 0.6);
      gs.globalCompositeOperation = "source-over";
    };

    const off = ctx.onFrame((now, dt) => {
      const t = ctx.tokens();
      const nextBand = px(t["--sw-floor"], 260);
      if (nextBand !== band || layer.clientWidth !== width) {
        band = nextBand;
        size();
      }
      const accent = t["--accent"];
      const cyan = t["--s1"];
      const bg = t["--bg"];
      const key = `${cyan}|${bg}`;
      if (key !== drawn) {
        drawStill(cyan, bg);
        drawn = key;
      }
      phase = (phase + (Math.min(dt, 1000) / 1000) * (STEP / CYCLE_S)) % STEP;
      const depth = band * 1.15; // the nearest row (z = 1) just below the bottom edge
      gr.setTransform(scale, 0, 0, scale, 0, 0);
      gr.clearRect(0, 0, width, band);
      gr.fillStyle = accent;
      for (let n = 0; ; n++) {
        const y = depth / (1 + n * STEP - phase);
        const strength = ROW_ALPHA * (y / band) ** 1.4;
        if (strength < FAINTEST) break;
        if (y >= band) continue;
        gr.globalAlpha = Math.min(1, strength);
        gr.fillRect(0, Math.round(y), width, y > band * 0.55 ? 2 : 1);
      }
      gr.globalAlpha = 1;
    }, FPS);

    return () => {
      off();
      still.remove();
      rows.remove();
    };
  },
};
