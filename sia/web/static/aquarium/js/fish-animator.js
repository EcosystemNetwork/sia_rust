const TWO_PI = Math.PI * 2;
const lerp = (a,b,t) => a+(b-a)*t;
const wrapAngle = (a) => { while(a>Math.PI)a-=TWO_PI; while(a<-Math.PI)a+=TWO_PI; return a; };

export class FishAnimator {
  constructor(fishId, spec, startX, startY, canvasW, canvasH) {
    this.fishId  = fishId;
    this.spec    = spec;
    this.canvasW = canvasW;
    this.canvasH = canvasH;
    this.x = startX; this.y = startY;
    this.heading = Math.random() > 0.5 ? 0 : Math.PI;
    this.targetX = startX; this.targetY = startY;
    this.speed   = 0;
    this._tailPhase = Math.random() * TWO_PI;
    const segCount = spec.body_undulation > 0.65 ? 10 : (spec.body_undulation > 0.35 ? 8 : 6);
    this._bodySegs  = new Array(segCount).fill(0);
    this._action    = 'idle_swim';
    this._actionTimer = 0;
    this._pendingAction = null;
    this._pectoralPhase = 0;
    this._mouthOpen = 0;
    this._wanderTimer = Math.random() * 3000;
    this._startX = startX; this._startY = startY;
    this._tailAngle = 0;
    const effort = (spec.body_length_px - 40) / 120;
    this._baseSpeed = 0.02 + effort * 0.05;
    this.animState = this._buildAnimState();
  }

  triggerAction(action, durationMs, target=null) {
    this._pendingAction = { action, durationMs, target };
  }

  update(dt) {
    if (this._pendingAction) {
      const p = this._pendingAction; this._pendingAction = null;
      this._startAction(p.action, p.durationMs, p.target);
    }
    if (this._actionTimer > 0) { this._actionTimer -= dt; if (this._actionTimer<=0) this._action='idle_swim'; }

    switch(this._action) {
      case 'stationary': case 'threat_display': this._updateStationary(dt); break;
      case 'feeding_bite': this._updateFeeding(dt); break;
      default: this._updateSwim(dt, this._action==='startle'||this._action==='attack_strike'); break;
    }
    this._updateTailWave(dt);
    if (this.spec.hover_mode) this._updatePectoral(dt);
    this.animState = this._buildAnimState();
  }

  _startAction(action, durationMs, target) {
    this._action = action; this._actionTimer = durationMs;
    if (target) { this.targetX = target.x*this.canvasW; this.targetY = target.y*this.canvasH; }
    if (action==='startle'||action==='attack_strike') this.speed = this._baseSpeed*4.0*this.canvasW;
    else if (action==='stationary'||action==='threat_display'||action==='feeding_bite') this.speed=0;
  }

  _updateSwim(dt, burst) {
    const cw=this.canvasW, ch=this.canvasH, sp=this.spec;
    this._wanderTimer -= dt;
    if (this._wanderTimer <= 0) {
      this._wanderTimer = 3000 + Math.random()*4000;
      const margin = ch*0.12;
      this.targetX = Math.random()*cw*0.85+cw*0.075;
      this.targetY = Math.max(margin, Math.min(ch-margin, this._startY+(Math.random()-0.5)*ch*0.12));
    }
    const dx=this.targetX-this.x, dy=this.targetY-this.y, dist=Math.sqrt(dx*dx+dy*dy);
    const desiredHeading = Math.atan2(dy,dx);
    const err = wrapAngle(desiredHeading-this.heading);
    this.heading = wrapAngle(this.heading + err*Math.min(1,(burst?0.12:0.04)*dt/16));
    const targetSpeed = burst ? this._baseSpeed*3.5*cw : this._baseSpeed*cw;
    this.speed = lerp(this.speed, dist>10 ? targetSpeed : 0, 0.05);
    this.x += Math.cos(this.heading)*this.speed*dt/1000;
    this.y += Math.sin(this.heading)*this.speed*dt/1000;
    const pad=sp.body_length_px*0.6;
    if (this.x<pad) this.targetX=cw*0.3;
    if (this.x>cw-pad) this.targetX=cw*0.7;
    if (this.y<pad) this.targetY=this._startY;
    if (this.y>ch-pad) this.targetY=this._startY;
    this._mouthOpen = lerp(this._mouthOpen, 0, 0.08);
  }

  _updateStationary(dt) {
    const t=Date.now()/1000;
    this.y = this._startY + Math.sin(t*0.8+this.fishId.charCodeAt(0))*this.canvasH*0.008;
    this.speed = 0;
    this._mouthOpen = lerp(this._mouthOpen, 0, 0.08);
  }

  _updateFeeding(dt) {
    const progress = Math.max(0, 1-this._actionTimer/3000);
    this._mouthOpen = progress < 0.5 ? progress*2 : (1-progress)*2;
    this.speed = 0;
  }

  _updateTailWave(dt) {
    const sp=this.spec, period=sp.stroke_period_frames*(1000/60), freq=TWO_PI/period;
    const isBurst=this._action==='startle'||this._action==='attack_strike';
    const isStill=this._action==='stationary'||this.speed<0.5;
    const amplitude = isStill ? sp.max_tail_angle_deg*0.15*(Math.PI/180)
      : isBurst ? sp.max_tail_angle_deg*1.6*(Math.PI/180)
      : sp.max_tail_angle_deg*(Math.PI/180);
    this._tailPhase += freq*dt;
    this._tailAngle  = Math.sin(this._tailPhase)*amplitude;
    const nSegs=this._bodySegs.length;
    for (let i=0;i<nSegs;i++) {
      this._bodySegs[i] = Math.sin(this._tailPhase-(i/nSegs)*sp.body_undulation*Math.PI)*sp.body_undulation*0.25;
    }
  }

  _updatePectoral(dt) {
    const freq=TWO_PI/(this.spec.stroke_period_frames*(1000/60));
    this._pectoralPhase += freq*1.2*dt;
  }

  _buildAnimState() {
    return {
      x: this.x, y: this.y, heading: this.heading,
      tailAngle: this._tailAngle, bodyOffsets: this._bodySegs,
      mouthOpen: this._mouthOpen, pectoralAngle: Math.sin(this._pectoralPhase),
      actionProgress: this._actionTimer>0 ? 1-this._actionTimer/5000 : 0,
      action: this._action,
    };
  }
}
