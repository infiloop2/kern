/* Capture mono PCM without recording the page's playback. Average samples
   into 16 kHz blocks; state spans render quanta and arbitrary device rates. */
class DictationCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.running = true;
    this.port.onmessage = event => {
      if (event.data !== "stop") return;
      this.running = false;
      this.flush();
      this.port.postMessage({ stopped: true });
    };
  }

  flush() {
    if (!this.samples.length) return;
    const pcm = new Int16Array(this.samples);
    this.samples = [];
    this.port.postMessage({ pcm }, [pcm.buffer]);
  }

  process(inputs) {
    if (!this.running) return false;
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) {
      this.sum += sample;
      this.count += 1;
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      this.samples.push(Math.round(Math.max(-1, Math.min(1, this.sum / this.count)) * 32767));
      this.sum = 0;
      this.count = 0;
      if (this.samples.length === 1600) this.flush();
    }
    return true;
  }
}
registerProcessor("kern-dictation", DictationCapture);
