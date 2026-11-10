// Tiny hand-rolled canvas chart for Cd/Cl time series — no chart library.
// plot({t, cd, cl}) draws auto-scaled axes with labels; cd tail mean shown
// as a dashed line so users can see the "settled" drag value.

const COL = { cd: "#35b6ff", cl: "#ff9f43", grid: "#2a3342",
              text: "#8b98a9", dash: "#3ddc84" };

export function plot(canvas, data) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || canvas.width;
  const H = canvas.clientHeight || canvas.height;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const t = data.t || [];
  const series = [
    { key: "cd", vals: data.cd, color: COL.cd, label: "Cd" },
    { key: "cl", vals: data.cl, color: COL.cl, label: "Cl" },
  ].filter(s => s.vals && s.vals.length === t.length && t.length > 1);
  if (!series.length) return null;

  const pad = { l: 44, r: 10, t: 8, b: 24 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const tmin = t[0], tmax = t[t.length - 1];
  let vmin = Infinity, vmax = -Infinity;
  for (const s of series)
    for (const v of s.vals) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
  if (!isFinite(vmin)) return null;
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const span = vmax - vmin;
  vmin -= span * 0.06; vmax += span * 0.06;

  const X = v => pad.l + ((v - tmin) / (tmax - tmin)) * iw;
  const Y = v => pad.t + (1 - (v - vmin) / (vmax - vmin)) * ih;

  // grid + axes
  ctx.strokeStyle = COL.grid;
  ctx.fillStyle = COL.text;
  ctx.font = "11px system-ui";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = vmin + (i / 4) * (vmax - vmin);
    ctx.beginPath(); ctx.moveTo(pad.l, Y(v)); ctx.lineTo(W - pad.r, Y(v));
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(v.toFixed(vmax - vmin < 1 ? 2 : 1), pad.l - 6, Y(v) + 3);
  }
  for (let i = 0; i <= 4; i++) {
    const tv = tmin + (i / 4) * (tmax - tmin);
    ctx.textAlign = "center";
    ctx.fillText(fmt(tv), X(tv), H - 8);
  }
  ctx.textAlign = "left";
  ctx.fillText("t (steps)", pad.l, H - 8);

  // curves
  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    s.vals.forEach((v, i) => i ? ctx.lineTo(X(t[i]), Y(v))
                               : ctx.moveTo(X(t[i]), Y(v)));
    ctx.stroke();
  }

  // tail mean of Cd (the number that actually gets quoted) + spread, so an
  // oscillating wake is never presented as one confident-looking average
  let tail = null, spread = null;
  const cd = series.find(s => s.key === "cd");
  if (cd) {
    const n = cd.vals.length;
    const seg = cd.vals.slice(Math.floor(n * 2 / 3));
    tail = seg.reduce((a, b) => a + b, 0) / seg.length;
    spread = Math.sqrt(seg.reduce((a, b) => a + (b - tail) ** 2, 0) / seg.length);
    ctx.strokeStyle = COL.dash;
    ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, Y(tail)); ctx.lineTo(W - pad.r, Y(tail));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // legend
  let lx = pad.l + 8;
  ctx.font = "12px system-ui";
  for (const s of series) {
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, pad.t + 4, 14, 3);
    ctx.fillText(s.label, lx + 18, pad.t + 9);
    lx += 18 + ctx.measureText(s.label).width + 16;
  }
  return { cd_tail: tail, cd_spread: spread };
}

function fmt(v) {
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(0) + "k";
  return v.toFixed(0);
}
