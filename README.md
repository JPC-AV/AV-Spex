# AV Spex

AV processing application for digital preservation

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/av_spex_the_logo.png?raw=true" alt="AV Spex logo"/>
</p>

AV Spex is a macOS application written in Python that helps process digital audio and video media created from analog sources. It confirms that digitized files conform to predetermined specifications and performs automated preservation actions: fixity checks, access file creation, metadata sidecars, and HTML reports.

Designed for audiovisual preservation workflows, AV Spex was developed with support from the Smithsonian's National Museum of African American History and Culture (NMAAHC).

---

## Quick Start

### Install (Homebrew)
```bash
brew tap JPC-AV/AV-Spex
brew install av-spex
```

### Launch the GUI
```bash
av-spex-gui
```

### Run on a directory
```bash
av-spex /path/to/video_files
```

---

## Requirements

macOS 13 (Ventura) and up

### Required Command Line Tools

The following command line tools must be installed separately. The macOS package manager [Homebrew](https://brew.sh/) is recommended:

- **[ExifTool](https://exiftool.org/)** — embedded metadata extraction
- **[FFmpeg](https://www.ffmpeg.org/)** — stream analysis, access file creation, stream hashing
- **[MediaConch](https://mediaarea.net/MediaConch)** — policy-based conformance validation
- **[MediaInfo](https://mediaarea.net/en/MediaInfo)** — container and stream metadata extraction
- **[MKVToolNix](https://mkvtoolnix.download/)** - a set of tools to create, alter and inspect Matroska files 
- **[QCTools](https://bavc.org/programs/preservation/preservation-tools/)** — per-frame video quality analysis
- **[mkvalidator](https://www.matroska.org/downloads/mkvalidator.html)** — verifies Matroska and WebM files for spec conformance

Install with Homebrew:
```bash
brew install exiftool ffmpeg mediaconch mediainfo mkvtoolnix qcli mkvalidator
```

The AV Spex GUI checks for all required dependencies at startup:

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/dependency_check_0102026.png?raw=true" alt="Dependency Check"/>
</p>

---

## Installation

There are three installation options:

### 1. DMG (Recommended)

Download the installer from the [latest release](https://github.com/JPC-AV/JPC_AV_videoQC/releases/latest).

### 2. Homebrew 

```bash
brew tap JPC-AV/AV-Spex
brew install av-spex
```

Verify the installation:
```bash
av-spex --help
```

### 3. From Source

Python 3.10 or higher is required.

<details>
<summary><span style="font-style: italic;">Click for instructions on creating a virtual environment (optional)</span></summary>

Creating a virtual environment is optional but recommended to avoid system-wide package conflicts.

**Using venv:**

```bash
python3 -m venv name_of_env
source ./name_of_env/bin/activate
```

**Using Conda:**

1. Install: `brew install --cask anaconda`
2. Add to PATH: `export PATH="/opt/homebrew/anaconda3/bin:$PATH"` (Apple Silicon) or `export PATH="/usr/local/anaconda3/bin:$PATH"` (Intel)
3. Initialize: `conda init zsh`
4. Create environment: `conda create -n JPC_AV python=3.10.13`

</details>

```bash
cd path-to/JPC_AV_videoQC
pip install .
av-spex --help
```

---

## GUI Usage

If using the homebrew/cli version, launch the GUI with the command:
```bash
av-spex-gui
```

The GUI has four tabs: **Import**, **Checks**, **Spex**, and **Complex**

### Import Tab

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/avspex_import_tab.png?raw=true" alt="AV Spex Import Tab"/>
</p>

The Import tab is where you select input directories for processing and manage configuration files. It includes options to import, export, or reset the Checks and Spex configurations as JSON files.

### Checks Tab

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/avspex_checks_tab.png?raw=true" alt="AV Spex Checks Tab"/>
</p>

The Checks tab controls which tools and processing steps are run. It includes:

- **Checks Profiles**: Apply a preset profile that configures a predefined set of tool options.
  - **Step 1**: Run and check ExifTool, FFprobe, MediaInfo, MediaTrace, mkvalidator, and MediaConch; embed and output fixity
  - **Step 2**: Run QCTools and qct-parse (bar detection, evaluate bars, thumbnail export, audio analysis, clamped levels, chroma phase errors) plus CLAMS bars/tone detection and frame analysis (bitplane check, border detection, BRNG, signalstats); check fixity and validate stream fixity; generate the HTML report
  - **Vendor**: Run the metadata tools and MediaConch without comparing them against Spex values, embed stream fixity, and generate the HTML report — for checking vendor-supplied files before the full QC pass
  - **Off**: Turn off all tools
- **Checks Options**: The individual settings, grouped by what they affect — **Input** (the container extension to look for), **Validation** (filename checking), **Outputs** (access copy and HTML report), **Fixity** (whole-file and embedded stream hashing, each with its own algorithm), and **Tools** (one box per metadata tool, each with a **Run Tool** option that generates the sidecar file and a **Check Tool** option that compares it against expected Spex values, plus MediaConch and its policy). Every checkbox carries its description inline.

When your selections are set, go to the **Import** tab and click **Check Spex!** to start processing.

### Spex Tab

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/avspex_spex_tab.png?raw=true" alt="AV Spex Spex Tab"/>
</p>

The Spex tab displays the expected metadata values that AV Spex validates against, as a two-column grid of cards — one per category: **Filename**, **Signal Flow**, **MediaInfo**, **Exiftool**, **FFprobe**, and **qct-parse Thresholds**. Each card shows:

- A **profile dropdown** listing that category's saved profiles, built-in and custom. Choosing one applies it immediately — there is no separate Apply step
- A **status line** naming the active profile, which reports *Modified from "profile name"* if you change expected values afterward rather than silently deselecting
- A one-line **summary** of the key expected values (e.g. `FFV1 · 720×486 · Interlaced BFF`)
- **View** / **Edit…** / **New…** / **Delete** buttons (see [Custom Metadata Profiles](#custom-metadata-profiles) below)

The qct-parse Thresholds card is read-only, with only a **View** button. The Signal Flow card applies only to MKV input and is grayed out for other containers, since its equipment chain is validated against embedded Matroska tags.

### Complex Tab

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/avspex_complex_tab.png?raw=true" alt="AV Spex Complex Tab"/>
</p>

The Complex tab configures the advanced analysis steps — typically run during Step 2 or configured independently. Options are grouped by what they check, rather than by the tool that implements them:

- **QCTools Report**: Run QCTools on the input video to generate the per-frame report that many of the checks below read, and set the report's file extension (`qctools.xml.gz` or `qctools.mkv`). If a report already exists in the `_qc_metadata` or `_vrecord_metadata` directory, it is reused instead of re-running.
- **Color Bars & Tone**:
  - **Detect Color Bars**: Find SMPTE color bars in the video content (via qct-parse). The detected section is used downstream to skip bars in BRNG analysis and trim them from the access file. Its sub-options **Evaluate Color Bars** (compare program content against a reference — either this file's own detected bars or standard SMPTE values) and **Export Thumbnails** (save thumbnails of failing frames) become available when detection is on.
  - **CLAMS Bars + Tone Detection**: Run the CLAMS SSIM-based SMPTE bars detector and cross-correlation tone detector together as one step, alongside qct-parse for side-by-side comparison (see [Audio Analysis & CLAMS Detection](#audio-analysis--clams-detection) below)
- **Video Signal Checks** (see [Frame Analysis](#frame-analysis) below for details on the frame analysis sub-steps):
  - **Detect Clamped Levels**: Broadcast-range level clamping from the analog-to-digital converter (via qct-parse)
  - **Detect Chroma Phase Errors**: Tape tracking artifacts where chroma collapses toward cyan or magenta (via qct-parse)
  - **Duplicate Frame Detection**: Runs of repeated frames likely caused by TBC or framesync errors
  - **Bitplane Check**: Verify that the 9th and 10th bits of 10-bit video contain data
  - **Border Detection**: Toggle on/off and select mode — simple (fixed pixel crop) or sophisticated (edge detection, with tunable parameters)
  - **Signalstats Analysis**: Enhanced FFprobe signalstats over the detected active area (requires Border Detection)
  - **BRNG Analysis**: Toggle on/off, set maximum analysis duration, and enable or disable automatic color bar skipping
  - **Analysis Periods**: The number and length of the time windows sampled across the video, shared by Signalstats and BRNG analysis
- **Audio Checks**:
  - **Audio Analysis**: Clipping, channel imbalance, identical channel detection, audible timecode (LTC), and audio dropout (via qct-parse)
  - **Tone Leak Detection**: A 1 kHz reference tone leaking from the transfer chain, heard as a faint high-pitched whine or squeak in quiet passages (via qct-parse)
  - **Dropped Sample Detection**: Potential audio sample drops from TBC/framesync or ADC devices

Checks marked "via qct-parse" read the QCTools report through the qct-parse tool, and enabling any of them turns qct-parse on automatically — there is no separate qct-parse "Run Tool" checkbox on this tab. Turning off all qct-parse-backed checks turns the tool off.

Once your selections are complete, return to the Import tab and click **Check Spex!**.

---

## Custom Metadata Profiles

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/avspex_mediainfo_profile_gui.png?raw=true" alt="AV Spex Custom MediaInfo Profile Window"/>
</p>

AV Spex supports custom profiles for five of the six Spex categories — **Filename**, **Signal Flow**, **MediaInfo**, **Exiftool**, and **FFprobe** — all managed the same way from that category's card in the Spex tab. (The qct-parse Thresholds card is read-only and has no profiles.) This is useful when processing collections with different technical specifications — for example, PAL vs. NTSC transfers, or FLAC vs. PCM audio.

Each profile-backed card includes:
- A **profile dropdown** to select from saved profiles, applied as soon as you choose one
- **View** to see every expected value for that category in a read-only window
- **New...** to define a new set of expected values, pre-filled from the current ones
- **Edit...** to modify an existing custom profile — or **Duplicate...** to copy a built-in profile into an editable custom one
- **Delete** to remove a custom profile

Default profiles are protected from modification and deletion: Edit becomes Duplicate and Delete is disabled for them, importing a config containing a profile named after a built-in adds it under a new name rather than replacing the original, and the shipped definitions are restored on every load. Custom profiles are saved to the user config directory and persist across sessions.

Multiple acceptable values can be defined for any field. For example, if a collection includes both FLAC and PCM audio, the expected `codec_name` can be set to `["flac", "pcm_s24le"]`.

Profiles can also be applied from the CLI:
```bash
av-spex --exiftool-profile "My ExifTool Profile"
av-spex --mediainfo-profile "My MediaInfo Profile"
av-spex --ffprobe-profile "My FFprobe Profile"
```

To view available profiles and current expected values:
```bash
av-spex -pp exiftool
av-spex -pp mediainfo
av-spex -pp ffprobe
```

---

## Audio Analysis & CLAMS Detection

AV Spex includes detection features for audio quality and SMPTE bars-and-tones segments. Both can be toggled in the **Complex** tab (the Audio Checks and Color Bars & Tone sections, respectively) or from the CLI.

### qct-parse Audio Analysis

When **Audio Analysis** is enabled (Audio Checks section of the Complex tab), AV Spex analyzes the audio track for:

- **Clipping** — samples at or near 0 dBFS that indicate the signal exceeded the digital ceiling
- **Channel imbalance** — significant level differences between left and right channels
- **Identical channels** — channels carrying the same content (dual mono), screened on level and phase agreement and confirmed by a sample-level comparison
- **Audible timecode** — timecode signal bleed into the audio track
- **Audio dropout** — extended silent or near-silent gaps that may indicate a tape or capture problem

Results are written to the per-file log and included in the HTML report.

Audio analysis runs inside qct-parse, so `qct_parse.run_tool` must be on. Both the GUI and the CLI auto-enable it — the Complex tab when the Audio Analysis box is checked, the CLI when `--enable-audio-analysis on` is passed.

```bash
av-spex --enable-audio-analysis on
```

### Tone Leak Detection

The **Tone Leak Detection** option (Audio Checks section of the Complex tab) flags a continuous ~1 kHz calibration tone leaking from the transfer chain into the recorded audio, heard as a faint high-pitched whine or squeak during quiet passages. Because the leaked tone is distorted, it leaves a harmonic comb at exact multiples of 1 kHz that stands above the surrounding spectral floor even when the tone is well below program level. Audio is decoded directly from the video file rather than read from the QCTools report, and every stream and channel is analyzed independently.

```bash
av-spex --enable-tone-leak-detection on
```

Like audio analysis, this runs inside qct-parse and the CLI auto-enables `qct_parse.run_tool` if needed.

### Clamped Levels Detection

The **Detect Clamped Levels** option (Video Signal Checks section of the Complex tab) detects broadcast-range level clamping introduced by some analog-to-digital converters, where signal that exceeded broadcast-legal range was hard-limited rather than preserved. It runs via qct-parse.

```bash
av-spex --enable-clamped-levels on
```

Like audio analysis, this runs inside qct-parse and the CLI auto-enables `qct_parse.run_tool` if needed.

### Chroma Phase Error Detection

The **Detect Chroma Phase Errors** option (Video Signal Checks section of the Complex tab) flags frames where the chroma signal has collapsed toward a single hue — typically cyan or magenta — usually caused by helical-scan tracking failures on tape sources, and often accompanied by horizontal displacement and a brief picture "swerve" at onset. Frames are flagged when both U and V span nearly the full chroma range, or when maximum saturation exceeds a bit-depth-aware threshold; consecutive flagged frames are merged into events, isolated transients are suppressed, and detected color bars are skipped. Events are reported with a thumbnail and the median hue at the peak-saturation frame.

```bash
av-spex --enable-chroma-phase-detection on
```

Like audio analysis, this runs inside qct-parse and the CLI auto-enables `qct_parse.run_tool` if needed.

### CLAMS Detection

**CLAMS** (Computational Linguistics Applications for Multimedia Services) is an open-source project led by Brandeis University. AV Spex adapts two CLAMS apps — [app-barsdetection](https://github.com/clamsproject/app-barsdetection) and [app-tonedetection](https://github.com/clamsproject/app-tonedetection) — porting just their detection cores into the AV Spex pipeline. CLAMS Detection runs the two together as a single step, before qct-parse:

- **SSIM bars detector** — uses the structural similarity index (SSIM) to identify SMPTE color bars by comparing frames against a reference pattern, providing a side-by-side comparison with qct-parse's own bars detector.
- **Cross-correlation tone detector** — identifies spans of monotonic audio, such as the 1 kHz tones that accompany SMPTE bars. Useful for locating bars-and-tones segments at the head of a tape.

Detected CLAMS regions are passed to qct-parse to guide additional windowed bars scans, and the head color-bars end time used for downstream BRNG-skip and access-file trim decisions is settled by a consensus between the two detectors. A bars span counts as *head* bars only if it starts within the first 30 seconds. When both detectors agree (their end times within 3 seconds), the later end wins; when they disagree, the disputed span is re-checked with SSIM, which decides — qct-parse works from luma statistics, which can't tell color bars from a bright, saturated slate. If only qct-parse found head bars and a CLAMS SSIM scan of that region says it isn't bars, the claim is dropped entirely and no end time is set, since a false trim would remove real program content.

```bash
av-spex --enable-clams-detection on
```

Numeric tuning of the CLAMS bars/tone parameters (SSIM threshold, sample ratio, minimum durations, etc.) is JSON-only — only the on/off toggle is exposed via the CLI. Edit the saved `last_used_checks_config.json` directly if you need to adjust those.

---

## Frame Analysis

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/avspex_frame_analysis_gui.png?raw=true" alt="AV Spex Frame Analysis Options"/>
</p>

AV Spex includes a frame analysis module for detecting common analog video artifacts. Each sub-step can be toggled independently from the Checks config (Complex tab in the GUI, or `--enable-*` flags on the CLI).

### Bitplane Check

Verifies that the 9th and 10th bits of 10-bit video contain data. Some TBC/framesync devices truncate these bits, producing what is effectively 8-bit video stored in a 10-bit container. The check flags clips where the high bits show no variation.

### Border Detection

Detects the active video area and identifies edge artifacts including head-switching noise at the bottom of the frame.

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/JPC_AV_01709_border_detection.jpg?raw=true" alt="Border Detection Example"/>
</p>

Two modes are available:
- **Simple** (default): Crops a fixed pixel border from each edge (default: 25px)
- **Sophisticated**: Uses edge detection to dynamically identify the active video area

### BRNG Analysis

Detects out-of-range luma and chroma values (BRNG — **B**roadcast **Ra**n**g**e) using a multi-method voting approach. Frames with violations are highlighted in the diagnostic output, and results are included in the HTML report. BRNG analysis automatically skips color bars at the head of the tape to avoid false positives.

<p align="center">
  <img src="https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/images_for_readme/avspex_brng_example.png?raw=true" alt="BRNG Analysis Example"/>
</p>

### Signalstats

Runs FFmpeg's `signalstats` filter over sampled time periods (default: 3 periods of 60 seconds each) to assess signal quality across the tape.

### Dropped Sample Detection

Detects potential audio sample drops introduced by TBC/framesync or ADC devices. AV Spex analyzes the audio track for spike patterns characteristic of dropped samples and compares the audio duration against the video duration to estimate sample loss. A spectrogram is generated for visual review and the results — including spike count, estimated loss, and spike timestamps — are included in the HTML report.

### Duplicate Frame Detection

Detects runs of repeated frames likely caused by TBC or framesync errors. AV Spex first uses QCTools' YDIF/UDIF/VDIF metrics to find candidate freezes (excluding color bars and black segments), then verifies each candidate with OpenCV. Detected runs are reported with their start time, duration, and length.

### Frame Analysis CLI Flags

```bash
av-spex --enable-bitplane-check {on,off}
av-spex --enable-border-detection {on,off}
av-spex --enable-brng-analysis {on,off}
av-spex --enable-signalstats {on,off}
av-spex --enable-dropped-sample-detection {on,off}
av-spex --enable-duplicate-frame-detection {on,off}
av-spex --frame-borders {simple,sophisticated}
av-spex --frame-border-pixels 25
av-spex --frame-brng-duration 300
av-spex --frame-no-colorbar-skip
```

---

## CLI Usage

```bash
av-spex [path/to/directory]
```

### Options

`av-spex --help` prints the full reference grouped by category (Config profiles, Config import/export, Tool toggles, qct-parse / CLAMS, Frame analysis, Input settings, Output settings, Fixity).

**Processing profiles:**
- `--profile {step1,step2,off,vendor}` — Apply a predefined processing profile (see [Checks Tab](#checks-tab) for details on each profile)

**Tool toggles:**
- `--on / --off` — Enable or disable individual tool options without affecting others. Format: `tool.run_tool` or `tool.check_tool` (e.g., `--on mediainfo.run_tool --on mediainfo.check_tool`)
- `--mediaconch-policy FILE` — Import a custom MediaConch XML policy file

**Spex profiles:**
- `--signalflow / -sn` — Select a signal flow equipment profile by name
- `--filename / -fn` — Select a filename convention profile by name
- `--exiftool-profile` — Apply a named ExifTool expected-values profile
- `--mediainfo-profile` — Apply a named MediaInfo expected-values profile
- `--ffprobe-profile` — Apply a named FFprobe expected-values profile
- `--exiftool-from-file FILE` / `--mediainfo-from-file FILE` / `--ffprobe-from-file FILE` — Create a new expected-values profile from a tool's raw output file (saves and applies it)

**qct-parse / CLAMS feature toggles:**
- `--enable-audio-analysis {on,off}` — Toggle qct-parse audio analysis (clipping, channel imbalance, identical channels, audible timecode, dropout). Auto-enables qct-parse if needed.
- `--enable-clamped-levels {on,off}` — Toggle broadcast-range level clamping detection. Auto-enables qct-parse if needed.
- `--enable-chroma-phase-detection {on,off}` — Toggle chroma phase error detection (tape tracking artifacts where chroma collapses toward cyan/magenta). Auto-enables qct-parse if needed.
- `--enable-tone-leak-detection {on,off}` — Toggle 1 kHz reference-tone leak detection. Auto-enables qct-parse if needed.
- `--enable-clams-detection {on,off}` — Toggle CLAMS SSIM bars + cross-correlation tone detector
- `--evaluate-bars-reference {detected,smpte}` — What Evaluate Color Bars grades against: this file's own detected bars (default) or standard SMPTE values. Only takes effect when `evaluateBars` is on.

**Frame analysis:**
- Six `--enable-*` sub-step toggles plus `--frame-borders`, `--frame-border-pixels`, `--frame-brng-duration`, and `--frame-no-colorbar-skip` — see [Frame Analysis CLI Flags](#frame-analysis-cli-flags) above

**Input settings:**
- `--video-file-extension {mkv,mov,mp4,avi,mxf}` — Which container to look for in the input directory (default: `mkv`). A non-MKV selection automatically turns off embedded stream fixity and the mediatrace custom-tag check, and skips the ffprobe signal flow (`ENCODER_SETTINGS`) check, since those only work on Matroska.

**Output settings:**
- `--access-trim-color-bars {on,off}` — Skip head color bars in the access file
- `--access-crop-borders {on,off}` — Crop the access file to the active picture area (requires `--access-crop-to-480 on`)
- `--access-crop-to-480 {on,off}` — Trim NTSC sources to 720x480; off keeps native 720x486
- `--access-exclude-flagged-audio {on,off}` — Exclude a channel flagged as silent or carrying audible timecode from the access file, outputting the good channel as dual mono (default: off; requires `--enable-audio-analysis on`)
- `--qctools-ext {qctools.xml.gz,qctools.mkv}` — QCTools output extension

**Fixity:**
- `--checksum-algorithm {md5,sha256}` — Hash algorithm for whole-file fixity (output / validate)
- `--stream-hash-algorithm {md5,sha256}` — Hash algorithm for embedded stream fixity

**Config management:**
- `--printprofile / -pp` — Print current config values. Accepts: `all`, `spex`, `checks`, `checks,outputs`, `checks,fixity`, `checks,tools`, `spex,filename_values`, `exiftool`, `mediainfo`, `ffprobe`, `signalflow`
- `--export-config {all,spex,checks}` — Export current config(s) to JSON
- `--export-file FILENAME` — Specify output filename for `--export-config`
- `--import-config FILE` — Import config from a previously exported JSON file
- `--export-mediaconch-policy [DEST]` — Export the current MediaConch policy XML so it can be shared. Takes an optional destination file or directory; with no argument it writes to the current directory.
- `--use-default-config` — Reset all configs to defaults

**Other:**
- `-d / --directory` — Indicate that the input paths are directories
- `-f / --file` — Indicate that the input paths are video files
- `-dr / --dryrun` — Apply config changes without processing any video files
- `--gui` — Force launch in GUI mode
- `--version` — Print the AV Spex version and exit

---

## Configuration

AV Spex's settings are stored in two primary JSON config files, editable through the GUI or CLI: the Checks config and the Spex config. Custom profiles live in their own files alongside them — filename profiles, signal flow profiles, and any custom ExifTool, MediaInfo, or FFprobe expected-value profiles. All of them are saved in the user config directory, so settings persist across sessions and survive an application update.

### Checks Config

Controls which tools run and what outputs are generated.

**Outputs**
- `access_file` — Create a low-resolution MP4 access copy
- `access_file_trim_color_bars` — Skip head color bars in the access file (requires qct-parse bars detection)
- `access_file_crop_borders` — Crop the access file to the detected active picture area (requires `access_file_crop_to_480: true`)
- `access_file_crop_to_480` — Trim NTSC to 720x480 (default `true`); set to `false` to keep native 720x486
- `access_file_exclude_flagged_audio` — Leave flagged audio channels out of the access copy: a channel found silent or carrying audible timecode is dropped, and dual mono is built from the good channel (default `false`; requires audio analysis)
- `report` — Generate an HTML summary report
- `qctools_ext` — Output extension for QCTools files (`qctools.xml.gz` or `qctools.mkv`)
- **Frame Analysis** settings: `enable_bitplane_check`, `enable_border_detection`, `enable_brng_analysis`, `enable_signalstats`, `enable_dropped_sample_detection`, `enable_duplicate_frame_detection`, `border_detection_mode` (simple/sophisticated), `simple_border_pixels` (default: 25), `brng_duration_limit` (default: 300 seconds), `brng_skip_color_bars`, `analysis_period_duration` and `analysis_period_count` (the periods shared by signalstats and BRNG analysis), `duplicate_min_run_length` (default: 2), plus sophisticated-border tuning fields and the border retry settings `auto_retry_borders` / `max_border_retries`

**Fixity**
- `output_fixity` — Write checksums to a fixity text file
- `check_fixity` — Validate against stored checksums
- `embed_stream_fixity` — Embed video/audio stream hashes into MKV tags
- `validate_stream_fixity` — Validate against embedded stream hashes
- `overwrite_stream_fixity` — Overwrite existing embedded hashes
- `checksum_algorithm` — Hash algorithm for whole-file fixity (`md5` or `sha256`)
- `stream_hash_algorithm` — Hash algorithm for embedded stream fixity (`md5` or `sha256`)

**Tools** — Each tool has `run_tool` and/or `check_tool` toggles:
- `exiftool`, `ffprobe`, `mediainfo`, `mediatrace`, `mkvalidator`: `run_tool` and `check_tool` (mkvalidator only applies to MKV inputs and is skipped for other containers)
- `mediaconch`: `run_mediaconch` and `mediaconch_policy` (path to XML policy file)
- `qctools`: `run_tool`
- `qct_parse`: `run_tool`, `barsDetection`, `evaluateBars`, `evaluateBarsReference` (`detected` or `smpte`), `thumbExport`, `audio_analysis`, `detect_clamped_levels`, `detect_chroma_phase_errors`, `detect_tone_leak`
- `clams_detection`: `run_tool` (numeric `bars` and `tone` sub-parameters are JSON-only)

**Input settings**
- `video_file_extension` — Which container AV Spex looks for in an input directory: `mkv` (default), `mov`, `mp4`, `avi`, or `mxf`. Embedded stream fixity and the custom Matroska tag checks only apply to MKV.
- `validate_filename` — Check input filenames against the active filename profile

### Spex Config

Stores expected metadata values organized by tool. Multiple acceptable values are supported using a list:
```json
"codec_name": ["flac", "pcm_s24le"]
```

Sections include: `filename_values`, `mediainfo_values` (general, video, audio track values), `exiftool_values`, `ffmpeg_values` (video stream, audio stream, format values), `mediatrace_values` (custom MKV tags like `ENCODER_SETTINGS`), `qct_parse_values` (color bar thresholds and content filter definitions), and `signalflow_profiles` (the equipment signal chain profiles selectable from the Spex tab).

### Managing Configs

To export or import configurations:
```bash
av-spex --export-config all --export-file my_config.json
av-spex --import-config my_config.json
```

Configs can also be imported, exported, and reset from the Import tab in the GUI.

---

## Outputs

For each processed input directory `{video_id}/`:

- **`{video_id}_qc_metadata/`** — Sidecar metadata files (ExifTool, FFprobe, MediaConch, MediaInfo, MediaTrace, QCTools), fixity files, and a per-file log
- **`{video_id}_report_csvs/`** — CSV files used to populate the HTML report
- **`{video_id}_avspex_report.html`** — HTML summary report
- **`{video_id}_vrecord_metadata/`** — Legacy vrecord metadata, moved here if present

### Logging

A per-file log is written inside each `_qc_metadata` directory:
`{video_id}_qc_metadata/{video_id}_avspex_processing.log`
Re-running a file appends to its existing log rather than overwriting it, so the full processing history of the file is preserved in one place. Each new run is separated from the previous one by a `NEW PROCESSING RUN` banner, followed by the run's start timestamp.

Each run also writes a timestamped application log, in a date-stamped subdirectory. The location depends on how AV Spex is running — the installed app writes to the user Logs directory, while running from source keeps logs beside the code:
```
# Installed app (DMG / Homebrew)
~/Library/Logs/AVSpex/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS_AVSpex.log

# Running from source
logs/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS_AVSpex.log
```

---

## Contributing

Contributions that enhance script functionality are welcome. Please ensure compatibility with Python 3.10 or higher.

---

## License

AV Spex is free software, distributed under the terms of the [GNU General Public License v3.0](LICENSE). The full license text is in the `LICENSE` file at the root of this repository.

Third-party code incorporated into AV Spex remains under its own license — see [Acknowledgements](#acknowledgements) below.

---

## Acknowledgements

AV Spex makes use of code from several open source projects. Attribution and copyright notices are included as comments inline where open source code is used, and each project's license terms are reproduced below.

[loglog](https://github.com/amiaopensource/loglog)
```
Copyright (C) 2021  Eddy Colloton and Morgan Morel

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License version 3 as published by
    the Free Software Foundation.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

[qct-parse](https://github.com/amiaopensource/qct-parse)
```
Copyright (C) 2016 Brendan Coates and Morgan Morel

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License version 3 as published by
    the Free Software Foundation.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

[IFIscripts](https://github.com/kieranjol/IFIscripts)
```
MIT License

    Copyright (c) 2015-2018 Kieran O'Leary for the Irish Film Institute.

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
    THE SOFTWARE.
```

[CLAMS](https://www.clams.ai/) — [app-barsdetection](https://github.com/clamsproject/app-barsdetection) and [app-tonedetection](https://github.com/clamsproject/app-tonedetection)
```
Copyright Brandeis University

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
```

The full upstream Apache 2.0 license text is included in the package at `src/AV_Spex/config/clams_bars/LICENSE`.