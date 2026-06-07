/**
 * Procedural 2D fish renderer.
 *
 * Rendering order:
 *   1. Behind-body fins (caudal, pectoral, anal) — body drawn on top masks their base
 *   2. Body
 *   3. Dorsal fin and finlets — drawn after body so they sit on its edge
 *   4. Surface details (lateral line, pattern, iridescence, eye)
 */

import { hexAlpha, darken } from './color-utils.js';

export class FishRenderer {
  constructor(spec) { this.spec = spec; }

  draw(ctx, anim, hungerLevel = 0) {
    const s = this.spec;
    const L = s.body_length_px;
    const H = L * s.body_depth_ratio;
    ctx.save();
    ctx.translate(anim.x, anim.y);

    // Prevent fish from appearing upside-down when heading left.
    // Fish body is drawn with nose at +x; when heading left we flip X instead of
    // rotating 180° so the dorsal fin always stays on top.
    const heading = anim.heading || 0;
    if (Math.cos(heading) < 0) {
      const tilt = -(Math.PI - heading); // tilt relative to left-pointing baseline
      ctx.rotate(tilt);
      ctx.scale(-1, 1);
    } else {
      ctx.rotate(heading);
    }

    const segs = anim.bodyOffsets || new Array(6).fill(0);
    const tailAngle = anim.tailAngle || 0;

    // -- Phase 1: behind-body fins (body will cover their attachment bases) --
    this._drawCaudalFin(ctx, s, L, H, tailAngle);
    if (s.hover_mode || s.pectoral_fin_role === 'primary') {
      this._drawPectoralFinsHover(ctx, s, L, H, anim.pectoralAngle || 0);
    } else {
      this._drawPectoralFin(ctx, s, L, H);
    }
    this._drawAnalFin(ctx, s, L, H);

    // -- Phase 2: body (masks fin attachment areas) --
    this._drawBody(ctx, s, L, H, segs, tailAngle);

    // -- Phase 3: dorsal fin + finlets on top of body --
    this._drawDorsalFin(ctx, s, L, H);
    if (s.has_finlets) this._drawFinlets(ctx, s, L, H);

    // -- Phase 4: surface details --
    if (s.lateral_line_color) this._drawLateralLine(ctx, s, L, H);
    if (s.iridescent) this._drawIridescence(ctx, s, L, H);
    if (s.pattern && s.pattern !== 'none') this._drawPattern(ctx, s, L, H);
    this._drawEye(ctx, s, L, H, anim.mouthOpen || 0);

    // Hunger overlay: desaturate + darken as fish approaches starvation
    if (hungerLevel > 0.45) {
      const intensity = (hungerLevel - 0.45) / 0.55;
      ctx.save();
      ctx.beginPath();
      this._bodyPath(ctx, s, L, H, new Array(6).fill(0), 0);
      ctx.fillStyle = `rgba(20,20,30,${intensity * 0.38})`;
      ctx.fill();
      ctx.restore();
      // Urgent pulse for critically hungry fish
      if (hungerLevel > 0.82) {
        const pulse = 0.5 + 0.5 * Math.sin(Date.now() * 0.008);
        ctx.save();
        ctx.beginPath();
        this._bodyPath(ctx, s, L, H, new Array(6).fill(0), 0);
        ctx.strokeStyle = `rgba(255,80,20,${pulse * 0.6})`;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.restore();
      }
    }

    ctx.restore();
  }

  // ---------------------------------------------------------------------------
  // Body
  // ---------------------------------------------------------------------------

  _drawBody(ctx, s, L, H, segs, tailAngle) {
    ctx.save();
    const grad = ctx.createLinearGradient(0, -H / 2, 0, H / 2);
    grad.addColorStop(0,    s.dorsal_color);
    grad.addColorStop(0.42, s.dorsal_color);
    grad.addColorStop(1,    s.ventral_color);
    ctx.beginPath();
    this._bodyPath(ctx, s, L, H, segs, tailAngle);
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.strokeStyle = hexAlpha(darken(s.dorsal_color, 0.35), 0.5);
    ctx.lineWidth = 0.7;
    ctx.stroke();
    ctx.restore();
  }

  _bodyPath(ctx, s, L, H, segs, tailAngle) {
    if (s.locomotion_type === 'anguilliform') { this._anguilliformPath(ctx, L, H); return; }
    if (s.locomotion_type === 'ostraciiform') { this._boxBodyPath(ctx, L, H); return; }
    this._fusiformPath(ctx, s, L, H, segs, tailAngle);
  }

  _fusiformPath(ctx, s, L, H, segs, tailAngle) {
    const hw = L / 2, hh = H / 2;
    const taper = s.locomotion_type === 'thunniform' ? 0.07 : 0.16;
    const tailH = H * taper;
    const waveX = s.body_undulation * (segs[segs.length - 1] || 0) * H * 0.28;
    ctx.beginPath();
    ctx.moveTo(hw * 0.88, 0);
    ctx.bezierCurveTo(hw * 0.6, -hh, -hw * 0.18, -hh * (1 + s.body_undulation * 0.28), -hw + waveX, -tailH);
    ctx.lineTo(-hw + waveX, tailH);
    ctx.bezierCurveTo(-hw * 0.18, hh * (1 + s.body_undulation * 0.28), hw * 0.6, hh, hw * 0.88, 0);
    ctx.closePath();
  }

  _anguilliformPath(ctx, L, H) {
    const hw = L / 2, hh = H / 2;
    ctx.beginPath();
    ctx.moveTo(hw * 0.7, 0);
    ctx.bezierCurveTo(hw * 0.4, -hh, -hw * 0.4, -hh * 0.6, -hw, -hh * 0.2);
    ctx.lineTo(-hw, hh * 0.2);
    ctx.bezierCurveTo(-hw * 0.4, hh * 0.6, hw * 0.4, hh, hw * 0.7, 0);
    ctx.closePath();
  }

  _boxBodyPath(ctx, L, H) {
    const hw = L / 2, hh = H / 2, r = Math.min(hw, hh) * 0.28;
    ctx.beginPath();
    ctx.moveTo(-hw + r, -hh); ctx.lineTo(hw * 0.6, -hh);
    ctx.quadraticCurveTo(hw * 0.88, -hh, hw * 0.88, 0);
    ctx.quadraticCurveTo(hw * 0.88, hh, hw * 0.6, hh);
    ctx.lineTo(-hw + r, hh);
    ctx.quadraticCurveTo(-hw, hh, -hw, hh - r);
    ctx.lineTo(-hw, -hh + r);
    ctx.quadraticCurveTo(-hw, -hh, -hw + r, -hh);
    ctx.closePath();
  }

  // ---------------------------------------------------------------------------
  // Caudal fin  (drawn behind body)
  // ---------------------------------------------------------------------------

  _drawCaudalFin(ctx, s, L, H, tailAngle) {
    const hw = L / 2;
    const halfSpan = H * 0.78;
    const depth = L * 0.24;
    const notch = s.tail_shape === 'lunate' ? 0.52 : (s.tail_shape === 'forked' ? 0.36 : 0.10);
    const color = s.fin_colors.caudal;

    ctx.save();
    ctx.translate(-hw, 0);
    ctx.rotate(tailAngle);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.bezierCurveTo(-depth * 0.55, -halfSpan * 0.45, -depth, -halfSpan * 0.82, -depth, -halfSpan);
    ctx.quadraticCurveTo(-depth * notch, 0, 0, 0);
    ctx.bezierCurveTo(-depth * 0.55, halfSpan * 0.45, -depth, halfSpan * 0.82, -depth, halfSpan);
    ctx.quadraticCurveTo(-depth * notch, 0, 0, 0);
    ctx.closePath();
    ctx.fillStyle = hexAlpha(color, 0.88);
    ctx.strokeStyle = hexAlpha(darken(color, 0.28), 0.6);
    ctx.lineWidth = 0.7;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  // ---------------------------------------------------------------------------
  // Pectoral fin  (single near-side fan, drawn behind body)
  // ---------------------------------------------------------------------------

  _drawPectoralFin(ctx, s, L, H) {
    const color = s.fin_colors.pectoral;
    const size = H * 0.52;

    ctx.save();
    // Attachment point: just behind gill area, slightly below the mid-line
    ctx.translate(L * 0.13, H * 0.07);

    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.bezierCurveTo(-size * 0.12, size * 0.42, -size * 0.48, size * 0.82, -size * 0.62, size * 0.68);
    ctx.bezierCurveTo(-size * 0.44, size * 0.22, -size * 0.18, size * 0.04, 0, 0);
    ctx.closePath();

    ctx.fillStyle = hexAlpha(color, 0.80);
    ctx.strokeStyle = hexAlpha(darken(color, 0.25), 0.55);
    ctx.lineWidth = 0.5;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  // Hover-mode pectorals (labriform / ostraciiform): large, oscillating, both sides
  _drawPectoralFinsHover(ctx, s, L, H, pectoralAngle) {
    const color = s.fin_colors.pectoral;
    const size = H * 0.78;

    for (const sign of [1, -1]) {
      ctx.save();
      ctx.translate(L * 0.09, sign * H * 0.06);
      ctx.rotate(sign * (0.28 + pectoralAngle * 0.55));
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.bezierCurveTo(-size * 0.12, sign * size * 0.38, -size * 0.42, sign * size * 0.78, -size * 0.58, sign * size * 0.62);
      ctx.bezierCurveTo(-size * 0.36, sign * size * 0.18, -size * 0.12, sign * size * 0.04, 0, 0);
      ctx.closePath();
      ctx.fillStyle = hexAlpha(color, 0.84);
      ctx.strokeStyle = hexAlpha(darken(color, 0.25), 0.55);
      ctx.lineWidth = 0.5;
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }
  }

  // ---------------------------------------------------------------------------
  // Anal fin  (drawn behind body)
  // ---------------------------------------------------------------------------

  _drawAnalFin(ctx, s, L, H) {
    const color = s.fin_colors.anal;
    const finH = H * 0.46;
    // Rear half of ventral: x from -L*0.2 to -L*0.0 (well within the body-width zone)
    const rearX = -L * 0.19;
    const frontX = -L * 0.01;
    const finW = frontX - rearX;
    const baseY = H / 2 - H * 0.05;  // slightly inside body bottom for seamless join

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(rearX, baseY);
    ctx.bezierCurveTo(rearX + finW * 0.22, baseY + finH, rearX + finW * 0.72, baseY + finH * 0.84, frontX, baseY);
    ctx.closePath();
    ctx.fillStyle = hexAlpha(color, 0.74);
    ctx.strokeStyle = hexAlpha(darken(color, 0.25), 0.45);
    ctx.lineWidth = 0.5;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  // ---------------------------------------------------------------------------
  // Dorsal fin  (drawn after body, sits on dorsal edge)
  // ---------------------------------------------------------------------------

  _drawDorsalFin(ctx, s, L, H) {
    const rayCount = Math.max(3, Math.min(s.dorsal_fin_rays || 8, 16));
    const color = s.fin_colors.dorsal;
    const isEel = s.locomotion_type === 'anguilliform';
    const finH = H * (isEel ? 0.42 : 0.68);

    // Keep fin base within the body's wide zone — away from nose where body narrows.
    // rearX ≈ -L*0.12, frontX ≈ L*0.16: both x positions are where body top ≈ -H/2.
    const rearX = -L * 0.12;
    const frontX = L * 0.16;
    const finW = frontX - rearX;
    // Extend base slightly BELOW the body edge so body fill merges seamlessly
    const baseY = -H / 2 + H * 0.05;

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(rearX, baseY);
    ctx.bezierCurveTo(
      rearX + finW * 0.14, baseY - finH,
      rearX + finW * 0.68, baseY - finH * 0.92,
      frontX, baseY
    );
    ctx.closePath();
    ctx.fillStyle = hexAlpha(color, 0.82);
    ctx.strokeStyle = hexAlpha(darken(color, 0.25), 0.6);
    ctx.lineWidth = 0.5;
    ctx.fill();
    ctx.stroke();

    // Fin rays
    ctx.strokeStyle = hexAlpha(darken(color, 0.15), 0.5);
    ctx.lineWidth = 0.4;
    for (let i = 0; i < rayCount; i++) {
      const t = i / (rayCount - 1);
      const bx = rearX + t * finW;
      const peakY = baseY - finH * Math.sin(t * Math.PI) * 0.9;
      ctx.beginPath();
      ctx.moveTo(bx, baseY);
      ctx.lineTo(bx, peakY);
      ctx.stroke();
    }
    ctx.restore();
  }

  // ---------------------------------------------------------------------------
  // Finlets
  // ---------------------------------------------------------------------------

  _drawFinlets(ctx, s, L, H) {
    const color = s.fin_colors.dorsal;
    for (let i = 0; i < 5; i++) {
      const x = -L * 0.28 - i * (L * 0.07);
      const size = L * 0.04 * (1 - i * 0.1);
      // Dorsal finlet
      ctx.save();
      ctx.translate(x, -H * 0.38);
      ctx.beginPath();
      ctx.moveTo(0, 0); ctx.lineTo(-size * 0.45, -size); ctx.lineTo(size * 0.45, -size);
      ctx.closePath();
      ctx.fillStyle = hexAlpha(color, 0.65); ctx.fill();
      ctx.restore();
      // Ventral finlet
      ctx.save();
      ctx.translate(x, H * 0.38);
      ctx.beginPath();
      ctx.moveTo(0, 0); ctx.lineTo(-size * 0.45, size); ctx.lineTo(size * 0.45, size);
      ctx.closePath();
      ctx.fillStyle = hexAlpha(color, 0.52); ctx.fill();
      ctx.restore();
    }
  }

  // ---------------------------------------------------------------------------
  // Lateral line
  // ---------------------------------------------------------------------------

  _drawLateralLine(ctx, s, L, H) {
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(L * 0.32, 0);
    ctx.bezierCurveTo(L * 0.1, H * 0.04, -L * 0.18, H * 0.05, -L * 0.44, H * 0.02);
    ctx.strokeStyle = hexAlpha(s.lateral_line_color, 0.52);
    ctx.lineWidth = 0.8;
    ctx.setLineDash([2, 3]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  }

  // ---------------------------------------------------------------------------
  // Iridescence overlay
  // ---------------------------------------------------------------------------

  _drawIridescence(ctx, s, L, H) {
    ctx.save();
    const grad = ctx.createRadialGradient(L * 0.08, 0, 0, 0, 0, L * 0.42);
    grad.addColorStop(0, 'rgba(200,240,255,0.18)');
    grad.addColorStop(0.5, 'rgba(160,230,210,0.08)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.beginPath();
    this._bodyPath(ctx, s, L, H, new Array(6).fill(0), 0);
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.restore();
  }

  // ---------------------------------------------------------------------------
  // Pattern overlay
  // ---------------------------------------------------------------------------

  _drawPattern(ctx, s, L, H) {
    const color = s.pattern_color || darken(s.dorsal_color, 0.28);
    ctx.save();
    ctx.beginPath();
    this._bodyPath(ctx, s, L, H, new Array(6).fill(0), 0);
    ctx.clip();
    switch (s.pattern) {
      case 'spotted':            this._drawSpots(ctx, L, H, color); break;
      case 'striped_horizontal': this._drawHStripes(ctx, L, H, color); break;
      case 'banded_vertical':    this._drawVBands(ctx, L, H, color); break;
      case 'mottled':            this._drawMottled(ctx, L, H, color); break;
    }
    ctx.restore();
  }

  _drawSpots(ctx, L, H, color) {
    const n = 6 + Math.floor(L / 20);
    ctx.fillStyle = hexAlpha(color, 0.42);
    for (let i = 0; i < n; i++) {
      const x = (i / n - 0.5) * L * 0.65 + Math.sin(i * 2.5) * L * 0.08;
      const y = Math.cos(i * 1.7) * H * 0.28;
      const r = H * (0.06 + Math.abs(Math.sin(i * 3.1)) * 0.06);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    }
  }

  _drawHStripes(ctx, L, H, color) {
    ctx.fillStyle = hexAlpha(color, 0.32);
    for (let i = 0; i < 3; i++) ctx.fillRect(-L * 0.5, (i / 3 - 0.28) * H * 0.9, L, H * 0.1);
  }

  _drawVBands(ctx, L, H, color) {
    // Three bands with a dark border pass then a bright fill pass — gives the
    // black-edged white stripes seen on banded fish like clownfish.
    const positions = [-0.28, 0.0, 0.26];
    const bandW = L * 0.09;
    ctx.fillStyle = hexAlpha(darken(color, 0.55), 0.50);
    for (const p of positions) ctx.fillRect(p * L - L * 0.055, -H * 0.6, bandW + L * 0.03, H * 1.2);
    ctx.fillStyle = hexAlpha(color, 0.72);
    for (const p of positions) ctx.fillRect(p * L - L * 0.04, -H * 0.6, bandW, H * 1.2);
  }

  _drawMottled(ctx, L, H, color) {
    ctx.fillStyle = hexAlpha(color, 0.26);
    for (let i = 0; i < 12; i++) {
      const x = Math.sin(i * 2.1) * L * 0.36, y = Math.cos(i * 1.9) * H * 0.3;
      const rw = L * (0.06 + Math.abs(Math.sin(i * 1.3)) * 0.08);
      const rh = H * (0.1 + Math.abs(Math.cos(i * 2.7)) * 0.12);
      ctx.beginPath(); ctx.ellipse(x, y, rw, rh, i * 0.5, 0, Math.PI * 2); ctx.fill();
    }
  }

  // ---------------------------------------------------------------------------
  // Eye + mouth
  // ---------------------------------------------------------------------------

  _drawEye(ctx, s, L, H, mouthOpen) {
    const ex = L * 0.3, ey = -H * 0.14, er = Math.max(1.5, H * 0.11);
    ctx.beginPath(); ctx.arc(ex, ey, er, 0, Math.PI * 2);
    ctx.fillStyle = '#f0f0e8'; ctx.fill();
    ctx.beginPath(); ctx.arc(ex + er * 0.1, ey, er * 0.56, 0, Math.PI * 2);
    ctx.fillStyle = '#101014'; ctx.fill();

    if (mouthOpen > 0.01) {
      const mx = L * 0.44;
      const gape = H * 0.11 * mouthOpen;
      const gapeW = s.mouth_gape === 'large' ? H * 0.1 : H * 0.065;
      ctx.save(); ctx.translate(mx, 0);
      ctx.beginPath(); ctx.ellipse(0, 0, gapeW, gape, 0, 0, Math.PI * 2);
      ctx.fillStyle = '#1a0a08'; ctx.fill(); ctx.restore();
    }
  }
}
