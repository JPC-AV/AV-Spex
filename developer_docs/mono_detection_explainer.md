1. Building the command

The goal is to get exactly the two channels under test out of the file as raw numbers, unaltered. For a normal single-stream file comparing channels 1 and 2:

ffmpeg -v error -i video.mkv \
  -map 0:a:0 -filter:a "pan=stereo|c0=c0|c1=c1" \
  -f f32le -acodec pcm_f32le pipe:1

pan is doing the channel selection, not mixing: c0=c0 means "output channel 0 is input channel 0, coefficient 1.0." Naming channels explicitly avoids any layout-driven downmix and lets me pull an arbitrary pair (channels 2 and 4, say) into a 2-channel stream. pan=stereo just fixes the output at two channels so the pipe has a known frame size.

For a multi-mono source (each channel a discrete stream) the streams are merged first, in the same order audio_stream_stats.py merged them for the sidecar, so "channel N" means the same thing in both places:

-filter_complex "[0:a:0][0:a:1][0:a:2][0:a:3]amerge=inputs=4,pan=stereo|c0=c0|c1=c2[cmp]" -map "[cmp]"

Two things deliberately absent: no -ar (resampling would rewrite every sample and destroy the bit-identical measurement) and no video mapping, so this is an audio-only decode.

Region: for a whole-file candidate there's no seek at all. For a partial candidate, -ss goes beformeasurement is a residual over tens of seconds, so the edge is immaterial.

2. Reading the pipe

Output is f32le — headerless, interleaved 32-bit floats, L R L R…. Float32 holds a 24-bit mantissa, so s16 and s24 PCM survive the conversion exactly; the comparison is lossless for the formats in play.

Reads are chunked at _IDENTICAL_CHUNK_SAMPLES (2¹⁸) sample frames = 2 MB per read, reshaped to an (N, 2) array. usable = len(buf) - (len(buf) % 8) trims a partial 8-byte sample frame — with Python's buffered read(n) that only happens at EOF, where the trailing fragment is dropped.

3. What's accumulated

Per chunk, in float64:
diff  = a - b          # straight duplicate cancels here
total = a + b          # polarity-inverted duplicate cancels here

peak_a, peak_b     = running max of |a|, |b|
peak_diff, peak_sum = running max of |a-b|, |a+b|
sumsq_diff, sumsq_sum += Σ(a-b)², Σ(a+b)²

Only these seven scalars and a sample count are kept, so memory is flat whether the file is 60 seconds or three hours. RMS is sqrt(sumsq / samples) at the end; everything converts to dBFS via 20·log₁₀, with linear zero mapping to -inf — that -inf is the whole point, since it means the residual is exactly zero, not merely small.

4. How the numbers become a verdict

_characterize_identical potherwise — then grades it: ≤ −100 dBFS is Bit-identical, ≤ program_peak_db − 30 dB is Effectively identical, louder than that is Distinct channels regardless of what the screen thought. program_peak_db (the louder channel's own peak) is the reference, so the test is relative to how loud the material actually is.

The measured contrast is stark in practice — from the verification runs on the same 60 s source:

┌────────────┬────────────────────────────────────┬─────────┬─────────────┐
│    file    │             diff peak              │  sum    │  program    │
│            │                                    │  peak   │    peak     │
├────────────┼────────────────────────────────────┼─────────┼─────────────┤
│ dual mono  │ −inf                               │ −9.7    │ −15.7       │
├────────────┼────────────────────────────────────┼─────────┼─────────────┤
│ inverted   │ −9.7                               │ −inf    │ −15.7       │
├────────────┼────────────────────────────────────┼─────────┼─────────────┤
│ real       │ (no decode — screen never          │         │             │
│ stereo     │ nominated it)                      │         │             │
└────────────┴────────────────────────────────────┴─────────┴─────────────┘

5. Failure handling

FileNotFoundError (no ffmpeg), a non-zero exit, or zero samples decoded all return None, which surfaces in the report as (unconfirmed) rather than a false assertion of identity. check_cancelled is polled between chunks and kills the process, so a long decode doesn't block a cancel.