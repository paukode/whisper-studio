class PCMProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input?.length) {
      this.port.postMessage(input);
    }
    return true;
  }
}

registerProcessor('pcm-processor', PCMProcessor);
