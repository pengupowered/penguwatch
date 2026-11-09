// Cinema: a faint warm light drifting over the velvet curtains at the page's edges (docs/themes.md, "Effects").
//
// Two canvases in the effect layer, each only as wide as a curtain (the skin's --cn-curtain, read in each frame; none on a
// phone, where there are no curtains) and as high as the window. In each, one soft pool of light rises and falls along the
// folds, slowly, the two sides out of step, like light from the stage catching the velvet. Colours are the theme's tokens.
// The marquee's bulbs are the skin's (CSS, gated on this effect running).

const FPS = 3;
const PERIOD_S = 23; // seconds for a pool of light to rise and fall once
const ALPHA = 0.16; // the brightest the light gets, at its centre

const px = (value) => {
  const n = parseFloat(value);
  return Number.isFinite(n) && n > 0 && n < 400 ? n : 0;
};

export default {
  start(layer, ctx) {
    const sides = ["left", "right"].map((side) => {
      const canvas = document.createElement("canvas");
      canvas.className = `cn-curtain cn-${side}`;
      layer.append(canvas);
      return { side, canvas, g: canvas.getContext("2d") };
    });
    let width = 0;
    let height = 0;
    let curtain = -1;
    let scale = 1;
    let time = 0;

    const size = () => {
      scale = Math.min(2, window.devicePixelRatio || 1);
      for (const s of sides) {
        s.canvas.width = Math.max(1, Math.round(curtain * scale));
        s.canvas.height = Math.max(1, Math.round(height * scale));
        s.canvas.style.width = `${curtain}px`;
        s.canvas.style.height = `${height}px`;
        s.canvas.style.left = s.side === "left" ? "0px" : `${width - curtain}px`;
      }
    };

    const off = ctx.onFrame((now, dt) => {
      const t = ctx.tokens();
      const nextCurtain = px(t["--cn-curtain"]);
      if (nextCurtain !== curtain || layer.clientWidth !== width || layer.clientHeight !== height) {
        curtain = nextCurtain;
        width = layer.clientWidth;
        height = layer.clientHeight;
        size();
      }
      if (!curtain) return;
      time += Math.min(dt, 1000) / 1000;
      const light = t["--accent"];
      for (const [i, s] of sides.entries()) {
        const { g } = s;
        g.setTransform(scale, 0, 0, scale, 0, 0);
        g.clearRect(0, 0, curtain, height);
        const phase = (time / PERIOD_S) * 2 * Math.PI + i * 2.4;
        const y = height * (0.5 + 0.34 * Math.sin(phase));
        const r = Math.max(curtain * 2.2, height * 0.28);
        // the light in the accent, then its alpha cut to a soft round pool (destination-in keeps the colour, so the pool's
        // edge never darkens toward a transparent black)
        g.globalCompositeOperation = "source-over";
        g.globalAlpha = ALPHA * (0.75 + 0.25 * Math.sin(phase * 2.3 + 1));
        g.fillStyle = light;
        g.fillRect(0, 0, curtain, height);
        const pool = g.createRadialGradient(curtain / 2, y, 0, curtain / 2, y, r);
        pool.addColorStop(0, light);
        pool.addColorStop(1, "transparent");
        g.globalCompositeOperation = "destination-in";
        g.globalAlpha = 1;
        g.fillStyle = pool;
        g.fillRect(0, 0, curtain, height);
        g.globalCompositeOperation = "source-over";
      }
    }, FPS);

    return () => {
      off();
      for (const s of sides) s.canvas.remove();
    };
  },
};
