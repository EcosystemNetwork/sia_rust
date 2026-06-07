import { FishRenderer }     from './fish-renderer.js';
import { FishAnimator }     from './fish-animator.js';
import { AquariumWsClient } from './ws-client.js';

const FOOD_FALL_SPEED = 0.08;   // canvas-height fractions per second
const FOOD_LIFE       = 5.0;    // seconds before food disappears if not eaten
const DEATH_FADE_MS   = 1800;   // duration of death fade-out animation

export class AquariumCanvas {
  constructor(canvas, aquariumId) {
    this._canvas  = canvas;
    this._ctx     = canvas.getContext('2d');
    this._id      = aquariumId;
    this._biome   = '';
    this._bgDeep   = '#0a1628';
    this._bgSurface = '#0a2848';
    this._fish    = new Map();   // fish_id → {renderer, animator, fedAt, hungerInterval, fullFraction, dying, dyingStart}
    this._food    = new Map();   // food_id → {x, y, age}
    this._bubbles = [];
    this._spawnBubbles(18);
    this._lastTime = null;
    this._running  = false;
    this._thoughtTimeout = null;
    this._ws = new AquariumWsClient(aquariumId, (msg) => this._onMessage(msg));
  }

  start() {
    this._ws.connect();
    this._running = true;
    requestAnimationFrame((t) => this._loop(t));
  }

  stop() {
    this._running = false;
    this._ws.stop();
  }

  _onMessage(msg) {
    switch(msg.type) {
      case 'aquarium_init': {
        this._biome      = msg.biome     || '';
        this._bgDeep     = msg.bg_deep   || '#0a1628';
        this._bgSurface  = msg.bg_surface|| '#0a2848';
        const label = this._canvas.parentElement?.querySelector('.aquarium-label');
        if (label && msg.biome) label.textContent = msg.biome;
        break;
      }
      case 'fish_add': {
        const spec = msg.render_spec;
        const cw=this._canvas.width||480, ch=this._canvas.height||300;
        const x=(msg.position?.x||0.5)*cw, y=(msg.position?.y||0.5)*ch;
        this._fish.set(msg.fish_id, {
          renderer:       new FishRenderer(spec),
          animator:       new FishAnimator(msg.fish_id, spec, x, y, cw, ch),
          fedAt:          Date.now() / 1000,
          hungerInterval: spec.hunger_interval || 160,
          fullFraction:   spec.full_fraction   || 0.35,
          dying:          false,
          dyingStart:     0,
          opacity:        1,
        });
        break;
      }
      case 'fish_action': {
        const entry = this._fish.get(msg.fish_id);
        if (entry) entry.animator.triggerAction(msg.action, msg.duration_ms, msg.target_position||null);
        break;
      }
      case 'fish_fed': {
        const entry = this._fish.get(msg.fish_id);
        if (entry) {
          entry.fedAt = msg.fed_at ?? (Date.now() / 1000);
          entry.animator.triggerAction('feeding_bite', 2500, null);
        }
        break;
      }
      case 'fish_died': {
        const entry = this._fish.get(msg.fish_id);
        if (entry) {
          entry.dying     = true;
          entry.dyingStart = performance.now();
        }
        break;
      }
      case 'food_drop': {
        const cw = this._canvas.width || 480;
        const ch = this._canvas.height || 300;
        this._food.set(msg.food_id, {
          xFraction: msg.x_fraction,
          y: 0,
          age: 0,
        });
        break;
      }
      case 'agent_thought':
        this._showThought(msg.message);
        break;
    }
  }

  _loop(timestamp) {
    if (!this._running) return;
    const dt = this._lastTime ? Math.min(timestamp-this._lastTime, 100) : 16;
    this._lastTime = timestamp;
    this._resize();
    this._draw(dt);
    requestAnimationFrame((t) => this._loop(t));
  }

  _resize() {
    const canvas=this._canvas, frame=canvas.parentElement;
    const w=frame.clientWidth, h=Math.round(w*0.625);
    if (canvas.width!==w || canvas.height!==h) {
      canvas.width=w; canvas.height=h;
      canvas.style.height=h+'px';
      this._fish.forEach(({animator})=>{ animator.canvasW=w; animator.canvasH=h; });
    }
  }

  _draw(dt) {
    const ctx=this._ctx, cw=this._canvas.width, ch=this._canvas.height;
    const now = performance.now();
    const dtSec = dt / 1000;

    // Background
    const grad=ctx.createLinearGradient(0,0,0,ch);
    grad.addColorStop(0, this._bgSurface);
    grad.addColorStop(0.4, this._bgDeep);
    grad.addColorStop(1, this._bgDeep);
    ctx.fillStyle=grad; ctx.fillRect(0,0,cw,ch);
    this._drawLightShaft(ctx,cw,ch);
    this._updateBubbles(dt,cw,ch);

    // Food particles
    this._updateFood(ctx, dtSec, cw, ch);

    // Fish
    const toDelete = [];
    this._fish.forEach((entry, fishId) => {
      const {renderer, animator, dying, dyingStart} = entry;
      animator.update(dt);

      // Compute hunger level for visual feedback (0 = just fed, 1 = starving)
      const elapsed   = Date.now()/1000 - entry.fedAt;
      const fullUntil = entry.hungerInterval * entry.fullFraction;
      const hungryWindow = entry.hungerInterval - fullUntil;
      const hungerLevel = Math.max(0, Math.min(1, (elapsed - fullUntil) / hungryWindow));

      ctx.save();
      if (dying) {
        const t = (now - dyingStart) / DEATH_FADE_MS;
        const opacity = Math.max(0, 1 - t);
        entry.opacity = opacity;
        ctx.globalAlpha = opacity;
        if (t >= 1) toDelete.push(fishId);
      }
      renderer.draw(ctx, animator.animState, hungerLevel);
      ctx.restore();
    });
    toDelete.forEach(id => this._fish.delete(id));
  }

  _updateFood(ctx, dtSec, cw, ch) {
    const toDelete = [];
    this._food.forEach((food, foodId) => {
      food.y   += FOOD_FALL_SPEED * dtSec;
      food.age += dtSec;
      if (food.age > FOOD_LIFE || food.y > 1.05) {
        toDelete.push(foodId);
        return;
      }
      // Draw food pellet
      const px = food.xFraction * cw;
      const py = food.y * ch;
      const alpha = Math.min(1, (FOOD_LIFE - food.age) / 1.5);
      ctx.save();
      ctx.globalAlpha = alpha;
      // Outer halo
      const g = ctx.createRadialGradient(px, py, 0, px, py, 5);
      g.addColorStop(0, 'rgba(220,160,60,0.9)');
      g.addColorStop(1, 'rgba(180,100,20,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI*2); ctx.fill();
      // Core pellet
      ctx.fillStyle = '#c87820';
      ctx.beginPath(); ctx.arc(px, py, 2.5, 0, Math.PI*2); ctx.fill();
      ctx.restore();
    });
    toDelete.forEach(id => this._food.delete(id));
  }

  _drawLightShaft(ctx,cw,ch) {
    const t=Date.now()/6000, cx=cw*(0.5+Math.sin(t)*0.2);
    const grad=ctx.createLinearGradient(cx,0,cx+ch*0.2,ch*0.6);
    grad.addColorStop(0,'rgba(180,230,255,0.07)');
    grad.addColorStop(0.7,'rgba(180,230,255,0.02)');
    grad.addColorStop(1,'rgba(0,0,0,0)');
    ctx.save(); ctx.beginPath();
    ctx.moveTo(cx-cw*0.04,0); ctx.lineTo(cx+cw*0.04,0);
    ctx.lineTo(cx+cw*0.18,ch*0.65); ctx.lineTo(cx-cw*0.12,ch*0.65);
    ctx.closePath(); ctx.fillStyle=grad; ctx.fill(); ctx.restore();
  }

  _spawnBubbles(n) {
    for (let i=0;i<n;i++) this._bubbles.push({
      x:Math.random(), y:0.9+Math.random()*0.1,
      r:0.003+Math.random()*0.004, speed:0.003+Math.random()*0.006,
      wobble:Math.random()*Math.PI*2,
    });
  }

  _updateBubbles(dt,cw,ch) {
    const ctx=this._ctx, s=dt/1000;
    ctx.save();
    for (const b of this._bubbles) {
      b.y-=b.speed*s; b.wobble+=s*1.5;
      if (b.y<-0.02) { b.y=1.02; b.x=Math.random(); }
      const px=(b.x+Math.sin(b.wobble)*0.008)*cw, py=b.y*ch, pr=b.r*Math.min(cw,ch);
      ctx.beginPath(); ctx.arc(px,py,pr,0,Math.PI*2);
      ctx.strokeStyle='rgba(180,220,255,0.3)'; ctx.lineWidth=0.5; ctx.stroke();
    }
    ctx.restore();
  }

  _showThought(message) {
    const el=this._canvas.parentElement?.querySelector('.agent-thought');
    if (!el) return;
    el.textContent=message; el.classList.add('visible');
    clearTimeout(this._thoughtTimeout);
    this._thoughtTimeout=setTimeout(()=>el.classList.remove('visible'),4000);
  }
}
