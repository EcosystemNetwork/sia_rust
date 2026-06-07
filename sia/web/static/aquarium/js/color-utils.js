export function lerpColor(c1, c2, t) {
  const r1 = parseInt(c1.slice(1,3),16), g1 = parseInt(c1.slice(3,5),16), b1 = parseInt(c1.slice(5,7),16);
  const r2 = parseInt(c2.slice(1,3),16), g2 = parseInt(c2.slice(3,5),16), b2 = parseInt(c2.slice(5,7),16);
  const r = Math.round(r1+(r2-r1)*t), g = Math.round(g1+(g2-g1)*t), b = Math.round(b1+(b2-b1)*t);
  return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${b.toString(16).padStart(2,'0')}`;
}
export function darken(hex, amount=0.3) { return lerpColor(hex,'#000000',amount); }
export function lighten(hex, amount=0.3) { return lerpColor(hex,'#ffffff',amount); }
export function hexAlpha(hex, alpha) {
  if (!hex || !hex.startsWith('#')) return `rgba(128,128,128,${alpha})`;
  const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${alpha})`;
}
export function safeColor(v, fallback='#888888') {
  if (!v || typeof v !== 'string') return fallback;
  if (v.startsWith('#') && (v.length===4||v.length===7)) return v;
  if (v.startsWith('rgb')) return v;
  return fallback;
}
