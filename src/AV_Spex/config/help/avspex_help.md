# AV Spex Help

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
- **[MKVToolNix](https://mkvtoolnix.download/)** — a set of tools to create, alter and inspect Matroska files
- **[QCTools](https://bavc.org/programs/preservation/preservation-tools/)** — per-frame video quality analysis

Install with Homebrew:
```bash
brew install exiftool ffmpeg mediaconch mediainfo mkvtoolnix qcli
```

The AV Spex GUI checks for all required dependencies at startup.

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

Then install AV Spex:

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

The Import tab is where you select input directories for processing and manage configuration files. It includes options to import, export, or reset the Checks and Spex configurations as JSON files.

### Checks Tab

The Checks tab controls which tools and processing steps are run. It includes:

- **Checks Profiles**: Apply a preset profile (Step 1, Step 2, or Off) that configures a predefined set of tool options.
  - **Step 1**: Run and check ExifTool, FFprobe, MediaInfo, MediaTrace, and MediaConch; embed and output fixity
  - **Step 2**: Run QCTools and qct-parse (bar detection, evaluate bars, thumbnail export); validate fixity; generate HTML report
  - **Off**: Turn off all tools
- **Checks Options**: Enable or disable individual tools and checks using checkboxes. Each tool has a **Run Tool** option (generates a sidecar file) and a **Check Tool** option (compares the sidecar output against expected Spex values).

Click **Check Spex!** to start processing.

### Spex Tab

The Spex tab displays the expected metadata values that AV Spex validates against, organized by tool. It includes:

- **Filename / Signal Flow**: Dropdown menus to select the active filename convention and signal flow equipment profiles
- **ExifTool / MediaInfo / FFprobe**: Dropdown menus to select named expected-values profiles for each metadata tool (see the Custom Metadata Profiles section below)
- **Open Section**: View the current expected values for any section (read-only for default profiles)

### Complex Tab

The Complex tab configures the advanced analysis steps — typically run during Step 2 or configured independently. Options are grouped by what they check, rather than by the tool that implements them:

- **QCTools Report**: Run QCTools on the input video to generate the per-frame report that many of the checks below read, and set the report's file extension (`qctools.xml.gz` or `qctools.mkv`). If a report already exists in the `_qc_metadata` or `_vrecord_metadata` directory, it is reused instead of re-running.
- **Color Bars & Tone**:
  - **Detect Color Bars**: Find SMPTE color bars in the video content (via qct-parse). The detected section is used downstream to skip bars in BRNG analysis and trim them from the access file. Its sub-options **Evaluate Color Bars** (compare program content against the detected bars) and **Export Thumbnails** (save thumbnails of failing frames) become available when detection is on.
  - **CLAMS Bars + Tone Detection**: Run the CLAMS SSIM-based SMPTE bars detector and cross-correlation tone detector together as one step, alongside qct-parse for side-by-side comparison (see the Audio Analysis & CLAMS Detection section below)
- **Video Signal Checks** (see the Frame Analysis section below for details on the frame analysis sub-steps):
  - **Detect Clamped Levels**: Broadcast-range level clamping from the analog-to-digital converter (via qct-parse)
  - **Detect Chroma Phase Errors**: Tape tracking artifacts where chroma collapses toward cyan or magenta (via qct-parse)
  - **Duplicate Frame Detection**: Runs of repeated frames likely caused by TBC or framesync errors
  - **Bitplane Check**: Verify that the 9th and 10th bits of 10-bit video contain data
  - **Border Detection**: Toggle on/off and select mode — simple (fixed pixel crop) or sophisticated (edge detection, with tunable parameters)
  - **Signalstats Analysis**: Enhanced FFprobe signalstats over the detected active area (requires Border Detection)
  - **BRNG Analysis**: Toggle on/off, set maximum analysis duration, and enable or disable automatic color bar skipping
  - **Analysis Periods**: The number and length of the time windows sampled across the video, shared by Signalstats and BRNG analysis
- **Audio Checks**:
  - **Audio Analysis**: Clipping, channel imbalance, audible timecode (LTC), and audio dropout (via qct-parse)
  - **Dropped Sample Detection**: Potential audio sample drops from TBC/framesync or ADC devices

Checks marked "via qct-parse" read the QCTools report through the qct-parse tool, and enabling any of them turns qct-parse on automatically — there is no separate qct-parse "Run Tool" checkbox on this tab (the Checks tab still exposes one). Turning off all qct-parse-backed checks turns the tool off.

Once your Spex selections are complete, navigate to the Checks tab and click **Check Spex!**.

---

## Custom Metadata Profiles

AV Spex supports custom profiles for ExifTool, MediaInfo, and FFprobe. This is useful when processing collections with different technical specifications — for example, PAL vs. NTSC transfers, or FLAC vs. PCM audio.

Each tool's section in the Spex tab includes:
- A **profile dropdown** to select from saved profiles
- **Create Custom Profile...** to define a new set of expected values
- **Edit Selected Profile...** to modify an existing custom profile

Default profiles are protected from accidental modification or deletion. Custom profiles are saved to the user config directory and persist across sessions.

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
- **Audible timecode** — timecode signal bleed into the audio track
- **Audio dropout** — extended silent or near-silent gaps that may indicate a tape or capture problem

Results are written to the per-file log and included in the HTML report.

Audio analysis runs inside qct-parse, so `qct_parse.run_tool` must be on. Both the GUI and the CLI auto-enable it — the Complex tab when the Audio Analysis box is checked, the CLI when `--enable-audio-analysis on` is passed.

```bash
av-spex --enable-audio-analysis on
```

### Clamped Levels Detection

The **Detect Clamped Levels** option (Video Signal Checks section of the Complex tab) detects broadcast-range level clamping introduced by some analog-to-digital converters, where signal that exceeded broadcast-legal range was hard-limited rather than preserved. It runs via qct-parse.

```bash
av-spex --enable-clamped-levels on
```

Like audio analysis, this runs inside qct-parse and the CLI auto-enables `qct_parse.run_tool` if needed.

### CLAMS Detection

CLAMS Detection runs two analyses together as a single step, independent of qct-parse:

- **SSIM bars detector** — uses the structural similarity index (SSIM) to identify SMPTE color bars by comparing frames against a reference pattern. Runs in parallel with qct-parse's own bars detector to provide a side-by-side comparison.
- **Cross-correlation tone detector** — identifies spans of monotonic audio, such as the 1 kHz tones that accompany SMPTE bars. Useful for locating bars-and-tones segments at the head of a tape.

CLAMS results complement qct-parse output, but qct-parse remains authoritative for downstream BRNG-skip and access-file color-bar trim decisions.

```bash
av-spex --enable-clams-detection on
```

Numeric tuning of the CLAMS bars/tone parameters (SSIM threshold, sample ratio, minimum durations, etc.) is JSON-only — only the on/off toggle is exposed via the CLI. Edit the saved `last_used_checks_config.json` directly if you need to adjust those.

---

## Frame Analysis

AV Spex includes a frame analysis module for detecting common analog video artifacts. Each sub-step can be toggled independently from the Checks config (Complex tab in the GUI, or `--enable-*` flags on the CLI).

### Bitplane Check

Verifies that the 9th and 10th bits of 10-bit video contain data. Some TBC/framesync devices truncate these bits, producing what is effectively 8-bit video stored in a 10-bit container. The check flags clips where the high bits show no variation.

### Border Detection

Detects the active video area and identifies edge artifacts including head-switching noise at the bottom of the frame.

Two modes are available:
- **Simple** (default): Crops a fixed pixel border from each edge (default: 25px)
- **Sophisticated**: Uses edge detection to dynamically identify the active video area

### BRNG Analysis

Detects out-of-range luma and chroma values (BRNG — **B**roadcast **Ra**n**g**e) using a multi-method voting approach. Frames with violations are highlighted in the diagnostic output, and results are included in the HTML report. BRNG analysis automatically skips color bars at the head of the tape to avoid false positives.

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

`av-spex --help` prints the full reference grouped by category (Config profiles, Config import/export, Tool toggles, qct-parse / CLAMS, Frame analysis, Output settings, Fixity).

**Processing profiles:**
- `--profile {step1,step2,off}` — Apply a predefined processing profile (see the Checks Tab section above for details on each profile)

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
- `--enable-audio-analysis {on,off}` — Toggle qct-parse audio analysis (clipping, channel imbalance, audible timecode, dropout). Auto-enables qct-parse if needed.
- `--enable-clamped-levels {on,off}` — Toggle broadcast-range level clamping detection. Auto-enables qct-parse if needed.
- `--enable-clams-detection {on,off}` — Toggle CLAMS SSIM bars + cross-correlation tone detector

**Output settings:**
- `--access-trim-color-bars {on,off}` — Skip head color bars in the access file
- `--access-crop-borders {on,off}` — Crop the access file to the active picture area (requires `--access-crop-to-480 on`)
- `--access-crop-to-480 {on,off}` — Trim NTSC sources to 720x480; off keeps native 720x486
- `--qctools-ext {qctools.xml.gz,qctools.mkv}` — QCTools output extension

**Fixity:**
- `--checksum-algorithm {md5,sha256}` — Hash algorithm for whole-file fixity (output / validate)
- `--stream-hash-algorithm {md5,sha256}` — Hash algorithm for embedded stream fixity

**Config management:**
- `--printprofile / -pp` — Print current config values. Accepts: `all`, `spex`, `checks`, `checks,outputs`, `checks,fixity`, `checks,tools`, `spex,filename_values`, `exiftool`, `mediainfo`, `ffprobe`, `signalflow`
- `--export-config {all,spex,checks}` — Export current config(s) to JSON
- `--export-file FILENAME` — Specify output filename for `--export-config`
- `--import-config FILE` — Import config from a previously exported JSON file
- `--use-default-config` — Reset all configs to defaults

**Other:**
- `-dr / --dryrun` — Apply config changes without processing any video files
- `--gui` — Force launch in GUI mode

---

## Configuration

AV Spex's settings are stored in two primary JSON config files, editable through the GUI or CLI.

### Checks Config

Controls which tools run and what outputs are generated.

**Outputs**
- `access_file` — Create a low-resolution MP4 access copy
- `access_file_trim_color_bars` — Skip head color bars in the access file (requires qct-parse bars detection)
- `access_file_crop_borders` — Crop the access file to the detected active picture area (requires `access_file_crop_to_480: true`)
- `access_file_crop_to_480` — Trim NTSC to 720x480 (default `true`); set to `false` to keep native 720x486
- `report` — Generate an HTML summary report
- `qctools_ext` — Output extension for QCTools files (`qctools.xml.gz` or `qctools.mkv`)
- **Frame Analysis** settings: `enable_bitplane_check`, `enable_border_detection`, `enable_brng_analysis`, `enable_signalstats`, `enable_dropped_sample_detection`, `enable_duplicate_frame_detection`, `border_detection_mode` (simple/sophisticated), `simple_border_pixels` (default: 25), `brng_duration_limit` (default: 300 seconds), `analysis_period_duration` and `analysis_period_count` (signalstats sampling), plus sophisticated-border tuning fields

**Fixity**
- `output_fixity` — Write checksums to a fixity text file
- `check_fixity` — Validate against stored checksums
- `embed_stream_fixity` — Embed video/audio stream hashes into MKV tags
- `validate_stream_fixity` — Validate against embedded stream hashes
- `overwrite_stream_fixity` — Overwrite existing embedded hashes
- `checksum_algorithm` — Hash algorithm for whole-file fixity (`md5` or `sha256`)
- `stream_hash_algorithm` — Hash algorithm for embedded stream fixity (`md5` or `sha256`)

**Tools** — Each tool has `run_tool` and/or `check_tool` toggles:
- `exiftool`, `ffprobe`, `mediainfo`, `mediatrace`: `run_tool` and `check_tool`
- `mediaconch`: `run_mediaconch` and `mediaconch_policy` (path to XML policy file)
- `qctools`: `run_tool`
- `qct_parse`: `run_tool`, `barsDetection`, `evaluateBars`, `thumbExport`, `audio_analysis`, `detect_clamped_levels`
- `clams_detection`: `run_tool` (numeric `bars` and `tone` sub-parameters are JSON-only)

### Spex Config

Stores expected metadata values organized by tool. Multiple acceptable values are supported using a list:
```json
"codec_name": ["flac", "pcm_s24le"]
```

Sections include: `filename_values`, `mediainfo_values` (general, video, audio track values), `exiftool_values`, `ffmpeg_values` (video stream, audio stream, format values), `mediatrace_values` (custom MKV tags like `ENCODER_SETTINGS`), and `qct_parse_values` (color bar thresholds and content filter definitions).

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
These per-file logs are overwritten if a file is re-run.

Each run also writes a timestamped application log:
```
/.../Library/Logs/AVSpex/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS_JPC_AV_log.log
```

---

## Acknowledgements

AV Spex makes use of code from several open source projects, including [loglog](https://github.com/amiaopensource/loglog), [qct-parse](https://github.com/amiaopensource/qct-parse), and [IFIscripts](https://github.com/kieranjol/IFIscripts). Attribution and copyright notices are included as comments inline where open source code is used, and full license texts are included in the project README.

AV Spex is distributed under the GNU General Public License v3.0.
