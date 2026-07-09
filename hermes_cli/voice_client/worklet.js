"use strict";

const DEFAULT_TARGET_SAMPLE_RATE = 16_000;
const DEFAULT_FRAME_SAMPLES = 320;

class HermesMicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();

    const processorOptions = options.processorOptions || {};
    this.targetSampleRate =
      Number(processorOptions.targetSampleRate) || DEFAULT_TARGET_SAMPLE_RATE;
    this.frameSamples =
      Number(processorOptions.frameSamples) || DEFAULT_FRAME_SAMPLES;
    this.sourceSamplesPerOutput = sampleRate / this.targetSampleRate;

    this.frame = new Int16Array(this.frameSamples);
    this.frameOffset = 0;
    this.frameSquareSum = 0;

    // Resampling positions are expressed in source-sample units. Keeping the
    // previous source sample and the next output position preserves phase
    // across the browser's 128-sample render quanta.
    this.hasPreviousSample = false;
    this.previousSample = 0;
    this.sourceSampleIndex = 0;
    this.nextOutputPosition = 0;
    this.running = true;

    this.port.onmessage = (event) => {
      if (event.data && event.data.type === "stop") {
        this.running = false;
      }
    };
  }

  emitSample(value) {
    const sample = Math.max(-1, Math.min(1, value));
    this.frame[this.frameOffset] =
      sample < 0 ? Math.round(sample * 0x8000) : Math.round(sample * 0x7fff);
    this.frameOffset += 1;
    this.frameSquareSum += sample * sample;

    if (this.frameOffset !== this.frameSamples) {
      return;
    }

    const pcm = this.frame;
    const rms = Math.sqrt(this.frameSquareSum / this.frameSamples);
    this.port.postMessage(
      {
        pcm: pcm.buffer,
        rms,
        // AudioContext clock, in seconds. The main thread uses its own
        // performance.now() clock for the measured barge-in reaction time.
        timestamp: currentTime,
      },
      [pcm.buffer],
    );

    this.frame = new Int16Array(this.frameSamples);
    this.frameOffset = 0;
    this.frameSquareSum = 0;
  }

  resample(sample) {
    if (!this.hasPreviousSample) {
      this.previousSample = sample;
      this.hasPreviousSample = true;
      return;
    }

    this.sourceSampleIndex += 1;
    const intervalStart = this.sourceSampleIndex - 1;
    const epsilon = 1e-9;

    while (this.nextOutputPosition <= this.sourceSampleIndex + epsilon) {
      const fraction = Math.max(
        0,
        Math.min(1, this.nextOutputPosition - intervalStart),
      );
      const interpolated =
        this.previousSample + (sample - this.previousSample) * fraction;
      this.emitSample(interpolated);
      this.nextOutputPosition += this.sourceSamplesPerOutput;
    }

    this.previousSample = sample;
  }

  process(inputs) {
    if (!this.running) {
      return false;
    }

    const channels = inputs[0];
    if (!channels || channels.length === 0 || channels[0].length === 0) {
      return true;
    }

    const blockLength = channels[0].length;
    for (let sampleIndex = 0; sampleIndex < blockLength; sampleIndex += 1) {
      let mixed = 0;
      let contributingChannels = 0;
      for (const channel of channels) {
        if (sampleIndex < channel.length) {
          mixed += channel[sampleIndex];
          contributingChannels += 1;
        }
      }
      if (contributingChannels > 0) {
        this.resample(mixed / contributingChannels);
      }
    }

    return true;
  }
}

registerProcessor("hermes-mic-processor", HermesMicProcessor);
