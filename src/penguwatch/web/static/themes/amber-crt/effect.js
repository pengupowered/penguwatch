// Amber CRT: a slow roll down the glass (docs/themes.md, "Effects").
//
// One canvas in the effect layer: a soft band of brighter phosphor with a few stronger scanlines in it, as wide as the
// window and a quarter of its height. The canvas is one pixel wide (the band only changes from top to bottom; the skin
// stretches it to the window's width), so redrawing it each frame costs next to nothing, and the frame moves it down by
// a transform: the compositor moves the canvas, the page is not repainted. The layer is behind the page, so the band
// shows on the glass between and around the cards and faintly through the see-through header, never over them. The
// static scanlines and the glow are the skin's (skin.css), there without the effect too.

const FPS = 6;
const ROLL_S = 16; // seconds for the band to cross the window
const BAND = 0.26; // the band's height, a share of the window's
const ALPHA = 0.07; // the band's strength at its middle
const GAP = 3; // px between the band's scanlines

export default {
  start(layer, ctx) {
    const canvas = document.createElement("canvas");
    canvas.className = "crt-roll";
    layer.append(canvas);
    const g = canvas.getContext("2d");
    let height = 0;
    let band = 0;
    let colour = "";
    let phase = 0;
    let scale = 1;

    const size = () => {
      scale = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = 1;
      canvas.height = Math.round(band * scale);
      canvas.style.height = `${band}px`;
    };
    const paint = () => {
      g.setTransform(1, 0, 0, scale, 0, 0);
      g.clearRect(0, 0, 1, band);
      const grad = g.createLinearGradient(0, 0, 0, band);
      grad.addColorStop(0, "transparent");
      grad.addColorStop(0.55, colour);
      grad.addColorStop(1, "transparent");
      g.globalAlpha = ALPHA;
      g.fillStyle = grad;
      g.fillRect(0, 0, 1, band);
      g.globalAlpha = ALPHA * 0.9;
      for (let y = 0; y < band; y += GAP) g.fillRect(0, y, 1, 1);
      g.globalAlpha = 1;
    };

    const off = ctx.onFrame((now, dt) => {
      const nextHeight = layer.clientHeight;
      const nextColour = ctx.tokens()["--accent"];
      if (nextHeight !== height || nextColour !== colour) {
        height = nextHeight;
        colour = nextColour;
        band = Math.max(80, Math.round(height * BAND));
        size();
      }
      paint();
      phase = (phase + Math.min(dt, 1000) / 1000 / ROLL_S) % 1;
      const y = Math.round(-band + phase * (height + band));
      canvas.style.transform = `translate3d(0, ${y}px, 0)`;
    }, FPS);

    return () => {
      off();
      canvas.remove();
    };
  },
};
