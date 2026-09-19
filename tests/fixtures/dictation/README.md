# Dictation smoke fixture

`jfk-four-seconds.wav` is the first four seconds of the public-domain recording
of US President John F. Kennedy's 1961 inaugural address: “And so, my fellow
Americans”. It was derived from OpenAI Whisper's `tests/jfk.flac`:
https://github.com/openai/whisper/blob/main/tests/jfk.flac

Upstream Git blob: `e44b7c13897eae7f78beb220c61fe77429a3961d`.
Converted using PyAV to mono PCM16 at 16 kHz and trimmed to 64,000 samples.
No operator recording is included. The live smoke checks broad expected words,
not punctuation or exact model output. No fixture download is needed at runtime.
