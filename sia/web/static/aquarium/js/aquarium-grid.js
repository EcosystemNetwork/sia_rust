import { AquariumCanvas } from './aquarium-canvas.js';

export class AquariumGrid {
  constructor(container, count) {
    this._container = container;
    this._count     = Math.max(1, Math.min(count, 12));
    this._canvases  = [];
  }
  init() {
    for (let i=0;i<this._count;i++) {
      const id    = `aq-${i}`;
      const frame = this._buildFrame(id, i);
      this._container.appendChild(frame);
      const canvas = new AquariumCanvas(frame.querySelector('canvas'), id);
      canvas.start();
      this._canvases.push(canvas);
    }
  }
  stop() {
    for (const c of this._canvases) c.stop();
    this._canvases = [];
  }
  _buildFrame(id, index) {
    const frame = document.createElement('div');
    frame.className = 'aquarium-frame';
    frame.dataset.id = id;
    const canvas = document.createElement('canvas');
    frame.appendChild(canvas);
    const label = document.createElement('div');
    label.className = 'aquarium-label';
    label.textContent = `Aquarium ${index+1}`;
    frame.appendChild(label);
    const thought = document.createElement('div');
    thought.className = 'agent-thought';
    frame.appendChild(thought);
    return frame;
  }
}
