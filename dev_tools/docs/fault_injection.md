# Fault injection

Review sheets measure how much of what a detector reports is real. They cannot measure what it
misses, and a detector that almost never fires gives a sheet nothing to judge. `avspex-dev
inject` makes the faults, so where they are is known, and counts how many the detector finds.

```bash
avspex-dev inject dropped-samples            # splices and digital-silence gaps
avspex-dev inject tone-leak                  # a leaked 1 kHz reference tone, at a range of levels
avspex-dev inject duplicate-frames           # freezes of 1 to 15 repeated frames
avspex-dev inject duplicate-frames --min-run-length 2     # the same, with a detector setting changed
avspex-dev inject dropped-samples --tapes JPC_AV_01954 --keep ~/clips    # keep the clips, to listen
```

These are the three detectors that read the video file itself, which is why
[the detector replay](replays.md#detector-replay) cannot cover them. The tapes' drive has to be
mounted: each tape's path comes from the run's `package.json`.

## What happens to each tape

1. A clip of the programme is read off the drive, starting 40% of the way through (`--at`
   overrides). Audio faults use the first audio stream only, as 24-bit PCM.
2. The detector is run on the clip untouched. That is the **control**: anything it reports there
   was already on the tape and is not counted as found or as a false alarm.
3. Faults are put in at recorded positions and sizes, and the detector is run again.
4. A fault is **found** when a detection lands within 0.05 s of it. Detections that match no
   fault and no control report are counted as "reports the injected faults do not account for".

| Fault | What is injected | Clip | Cost per tape |
|---|---|---|---|
| `dropped-samples` | One file with audio cut out and butted together (0.3, 1, 2, 10 video frames' worth) every 5 s; one with every channel set to exact zero for 2 ms to 1 s | 120 s | ~20 s |
| `tone-leak` | A steady 1 kHz sine on channel 0 for the whole clip, with and without harmonics at 2, 3, 4, 6 kHz, each at -50 to -100 dBFS peak. One file per level; found when the detector flags the channel | 128 s | ~20 s |
| `duplicate-frames` | A frame held for 1, 2, 3, 5 or 15 repeats every 3 s, replacing the frames after it. Once bit for bit, once with fresh noise on every repeat (a freeze the deck re-digitizes) | 45 s | ~4 min: three QCTools passes |

Splices are reported by how far the step at the join stands out from the sample-to-sample
movement of the programme just before it, since that, not the amount of audio lost, is what
there is to detect. Freezes record the picture change they hide: a freeze over a still picture
hides nothing.

## Reading the result

- **A miss is a real miss.** An injected fault is the fault's defining feature in its plainest
  form: an exact butt joint, exact zeros, a steady sine, a held frame.
- **A hit says less.** Real faults are messier: a real leak wobbles, a real splice can land in a
  fade. Recall on real tapes will be at or below these figures.
- **The noise on a re-digitized freeze is a model.** `REDIGITIZED_NOISE_SIGMA` is set so
  repeat-to-repeat YDIF is about 0.7, the middle of what the verified LC freezes measure. The
  detector's limit is 1.0; at a sigma of 0.9 (YDIF about 1.0) it found none. A noisier capture
  chain than the LC one would behave like that.

## Rules

- Tapes held out for the detector in the ledger are never used.
- Running the detector on a clip is looking at that tape's results for it. The command lists the
  tapes that had not been looked at before, with the `avspex-dev ledger add ... dev` line that
  records them.
- `--seed` picks the tapes and the fault positions, so a result can be repeated. The default is
  today's date.
- Results go to `out/inject/<fault>-<code>-s<seed>/` in the corpus, untracked: `faults.csv` has
  one row per injected fault with its size and whether it was found; `run.json` has the tapes,
  clip starts and code.

To add a fault: a pure function in `_inject.py` that returns the changed signal and the list
of what was injected, a `run_<fault>` that applies it and runs the detector, and entries in
`RUNNERS`, `TOPICS`, `DEFAULT_SECONDS` and `inject.REPORTS`. Audible timecode is not covered:
it would need an LTC generator and a QCTools pass per clip.
