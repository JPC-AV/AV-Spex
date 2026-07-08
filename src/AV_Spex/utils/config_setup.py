from dataclasses import dataclass, field
from typing import List, Dict, Union, Optional
from enum import Enum

@dataclass
class FilenameSection:
    value: str
    section_type: str = field(default="literal") 

@dataclass
class FilenameValues:
    fn_sections: Dict[str, FilenameSection]
    FileExtension: str

@dataclass
class MediainfoGeneralValues:
    FileExtension: str
    Format: str
    OverallBitRate_Mode: str

@dataclass
class MediainfoVideoValues:
    Format: str
    Format_Settings_GOP: str
    CodecID: List[str]
    Width: str
    Height: str
    PixelAspectRatio: str
    DisplayAspectRatio: str
    FrameRate_Mode_String: str
    FrameRate: str
    Standard: str
    ColorSpace: str
    ChromaSubsampling: str
    BitDepth: str
    ScanType: str
    ScanOrder: str
    Compression_Mode: str
    colour_primaries: str
    colour_primaries_Source: str
    transfer_characteristics: str
    transfer_characteristics_Source: str
    matrix_coefficients: str
    MaxSlicesCount: List[str]
    ErrorDetectionType: str

@dataclass
class MediainfoAudioValues:
    Format: List[str]
    Channels: str
    SamplingRate: str
    BitDepth: str
    Compression_Mode: str

@dataclass
class ExiftoolValues:
    FileType: str
    FileTypeExtension: str
    MIMEType: str
    VideoFrameRate: str
    ImageWidth: str
    ImageHeight: str
    VideoScanType: str
    DisplayWidth: str
    DisplayHeight: str
    DisplayUnit: str
    CodecID: List[str]
    AudioChannels: str
    AudioSampleRate: str
    AudioBitsPerSample: str

@dataclass
class FFmpegVideoStream:
    codec_name: str
    codec_long_name: str
    codec_type: str
    codec_tag_string: List[str]
    codec_tag: List[str]
    width: str
    height: str
    display_aspect_ratio: str
    pix_fmt: str
    color_space: str
    color_transfer: str
    color_primaries: str
    field_order: str
    bits_per_raw_sample: str

@dataclass
class FFmpegAudioStream:
    codec_name: List[str]
    codec_long_name: List[str]
    codec_type: str
    codec_tag: str
    sample_fmt: str
    sample_rate: str
    channels: str
    channel_layout: str
    bits_per_raw_sample: str

@dataclass
class FFmpegFormat:
    format_name: str
    format_long_name: str
    tags: Dict[str, Optional[str]] = field(default_factory=lambda: {
        'creation_time': None,
        'ENCODER': None,
        'TITLE': None,
        'ENCODER_SETTINGS': EncoderSettings,
        'DESCRIPTION': None,
        'ORIGINAL MEDIA TYPE': None,
        'ENCODED_BY': None
    })

@dataclass
class EncoderSettings:
    Source_VTR: List[str] = field(default_factory=list)
    TBC_Framesync: List[str] = field(default_factory=list)
    ADC: List[str] = field(default_factory=list)
    Capture_Device: List[str] = field(default_factory=list)
    Computer: List[str] = field(default_factory=list)

@dataclass
class MediaTraceValues:
    COLLECTION: Optional[str]
    TITLE: Optional[str]
    CATALOG_NUMBER: Optional[str]
    DESCRIPTION: Optional[str]
    DATE_DIGITIZED: Optional[str]
    ENCODER_SETTINGS: EncoderSettings
    ENCODED_BY: Optional[str]
    ORIGINAL_MEDIA_TYPE: Optional[str]
    DATE_TAGGED: Optional[str]
    TERMS_OF_USE: Optional[str]
    _TECHNICAL_NOTES: Optional[str]
    _ORIGINAL_FPS: Optional[str]

@dataclass
class AllBlackContent:
    YMAX: tuple[float, str]
    YHIGH: tuple[float, str]
    YLOW: tuple[float, str]
    YMIN: tuple[float, str]

@dataclass
class StaticContent:
    YMIN: tuple[float, str]
    YLOW: tuple[float, str]
    YAVG: tuple[float, str]
    YMAX: tuple[float, str]
    YDIF: tuple[float, str]
    ULOW: tuple[float, str]
    UAVG: tuple[float, str]
    UHIGH: tuple[float, str]
    UMAX: tuple[float, str]
    UDIF: tuple[float, str]
    VMIN: tuple[float, str]
    VLOW: tuple[float, str]
    VAVG: tuple[float, str]
    VHIGH: tuple[float, str]
    VMAX: tuple[float, str]
    VDIF: tuple[float, str]

@dataclass
class Content:
    allBlack: AllBlackContent
    static: StaticContent

@dataclass
class FullTagList:
    YMIN: Optional[float]
    YLOW: Optional[float]
    YAVG: Optional[float]
    YHIGH: Optional[float]
    YMAX: Optional[float]
    UMIN: Optional[float]
    ULOW: Optional[float]
    UAVG: Optional[float]
    UHIGH: Optional[float]
    UMAX: Optional[float]
    VMIN: Optional[float]
    VLOW: Optional[float]
    VAVG: Optional[float]
    VHIGH: Optional[float]
    VMAX: Optional[float]
    SATMIN: Optional[float]
    SATLOW: Optional[float]
    SATAVG: Optional[float]
    SATHIGH: Optional[float]
    SATMAX: Optional[float]
    HUEMED: Optional[float]
    HUEAVG: Optional[float]
    YDIF: Optional[float]
    UDIF: Optional[float]
    VDIF: Optional[float]
    TOUT: Optional[float]
    VREP: Optional[float]
    BRNG: Optional[float]
    mse_y: Optional[float]
    mse_u: Optional[float]
    mse_v: Optional[float]
    mse_avg: Optional[float]
    psnr_y: Optional[float]
    psnr_u: Optional[float]
    psnr_v: Optional[float]
    psnr_avg: Optional[float]
    Overall_Min_level: Optional[float]
    Overall_Max_level: Optional[float]

@dataclass
class SmpteColorBars:
    YMAX: float
    YMIN: float
    UMIN: float
    UMAX: float
    VMIN: float
    VMAX: float
    SATMIN: float
    SATMAX: float

@dataclass
class QCTParseValues:
    fullTagList: FullTagList
    smpte_color_bars: SmpteColorBars

@dataclass
class SpexConfig:
    filename_values: FilenameValues
    mediainfo_values: Dict[str, Union[MediainfoGeneralValues, MediainfoVideoValues, MediainfoAudioValues]]
    exiftool_values: ExiftoolValues
    ffmpeg_values: Dict[str, Union[FFmpegVideoStream, FFmpegAudioStream, FFmpegFormat]]
    mediatrace_values: MediaTraceValues
    qct_parse_values: QCTParseValues
    signalflow_profiles: Dict[str, Dict] = field(default_factory=dict)

@dataclass
class FrameAnalysisConfig:
    '''Configuration for frame analysis (borders, BRNG violations, signalstats)'''
    
    # Individual sub-step enable flags (now using bool to match JSON config)
    enable_bitplane_check: bool = True
    enable_border_detection: bool = True
    enable_brng_analysis: bool = True
    enable_signalstats: bool = True
    
    # Border detection settings
    border_detection_mode: str = "simple"
    simple_border_pixels: int = 25
    
    # Sophisticated border detection parameters
    sophisticated_threshold: int = 10
    sophisticated_edge_sample_width: int = 100
    sophisticated_sample_frames: int = 30
    sophisticated_padding: int = 5
    auto_retry_borders: bool = True
    max_border_retries: int = 3
    
    # BRNG analysis settings
    brng_duration_limit: int = 300
    brng_skip_color_bars: bool = True
    
    # Analysis period settings (used by signalstats and BRNG analysis)
    analysis_period_duration: int = 60
    analysis_period_count: int = 3

    # Dropped sample detection settings
    enable_dropped_sample_detection: bool = True

    # Duplicate frame detection settings
    enable_duplicate_frame_detection: bool = True
    duplicate_min_run_length: int = 2

# Output configuration
VALID_QCTOOLS_EXTENSIONS = ("qctools.xml.gz", "qctools.mkv")

# Supported input video container extensions. MKV is the historical default and the
# only container that supports the embedded stream-fixity / custom Matroska tag features.
SUPPORTED_VIDEO_EXTENSIONS = ("mkv", "mov", "mp4", "avi", "mxf")


def is_mkv_extension(ext: str) -> bool:
    """True if the given extension string refers to a Matroska (.mkv) container."""
    return ext.lower().lstrip('.') == "mkv"

# Output configuration
@dataclass
class OutputsConfig:
    access_file: bool
    report: bool
    qctools_ext: str # Must be one of VALID_QCTOOLS_EXTENSIONS
    frame_analysis: FrameAnalysisConfig = field(default_factory=FrameAnalysisConfig)
    access_file_trim_color_bars: bool = True
    access_file_crop_borders: bool = True
    access_file_crop_to_480: bool = True
    access_file_exclude_flagged_audio: bool = False

@dataclass
class ChecksumAlgorithm(Enum):
    """Supported checksum algorithms for fixity checking"""
    MD5 = "md5"
    SHA256 = "sha256"

# Fixity configuration 
@dataclass
class FixityConfig:
    check_fixity: bool
    validate_stream_fixity: bool
    embed_stream_fixity: bool
    output_fixity: bool
    overwrite_stream_fixity: bool
    checksum_algorithm: str = "md5"
    stream_hash_algorithm: str = "md5"

# Tool-specific configurations 
@dataclass
class BasicToolConfig:
    check_tool: bool
    run_tool: bool

@dataclass
class QCToolsConfig:
    run_tool: bool

@dataclass
class MediaConchConfig:
    mediaconch_policy: str  
    run_mediaconch: bool

@dataclass
class QCTParseToolConfig:
    run_tool: bool
    barsDetection: bool
    evaluateBars: bool
    thumbExport: bool
    audio_analysis: bool = False
    detect_clamped_levels: bool = False
    detect_chroma_phase_errors: bool = False
    detect_tone_leak: bool = False

@dataclass
class ClamsBarsParams:
    threshold: float = 0.7
    sample_ratio: int = 30
    stop_at_frame: int = 9000
    min_frame_count: int = 10
    stop_after_one: bool = True
    merge_gap_seconds: float = 1.0

@dataclass
class ClamsToneParams:
    tolerance: float = 1.0
    min_tone_duration_ms: int = 2000
    stop_at_seconds: int = 3600
    merge_gap_seconds: float = 5.0

@dataclass
class ClamsDetectionConfig:
    run_tool: bool = False
    bars: ClamsBarsParams = field(default_factory=ClamsBarsParams)
    tone: ClamsToneParams = field(default_factory=ClamsToneParams)

@dataclass
class ToolsConfig:
    exiftool: BasicToolConfig
    ffprobe: BasicToolConfig
    mediaconch: MediaConchConfig
    mediainfo: BasicToolConfig
    mediatrace: BasicToolConfig
    qctools: QCToolsConfig
    qct_parse: QCTParseToolConfig
    mkvalidator: BasicToolConfig
    clams_detection: ClamsDetectionConfig = field(
        default_factory=ClamsDetectionConfig
    )

@dataclass
class ChecksConfig:
    outputs: OutputsConfig
    fixity: FixityConfig
    tools: ToolsConfig
    validate_filename: bool = True
    video_file_extension: str = "mkv"

@dataclass
class FilenameProfile:
    fn_sections: Dict[str, FilenameSection]
    FileExtension: str

@dataclass
class FilenameConfig:
    filename_profiles: Dict[str, FilenameProfile]

@dataclass
class SignalflowProfile:
    """Profile for signal flow equipment configuration"""
    name: str
    Source_VTR: List[str] = field(default_factory=list)
    TBC_Framesync: List[str] = field(default_factory=list)
    ADC: List[str] = field(default_factory=list)
    Capture_Device: List[str] = field(default_factory=list)
    Computer: List[str] = field(default_factory=list)

@dataclass
class SignalflowConfig:
    """Container for signal flow profiles"""
    signalflow_profiles: Dict[str, SignalflowProfile] = field(default_factory=dict)

@dataclass
class ChecksProfile:
    """Custom profile for checks configuration"""
    name: str
    description: str = ""
    validate_filename: bool = True  
    outputs: OutputsConfig = field(default_factory=lambda: OutputsConfig(
        access_file=False,
        report=False, 
        qctools_ext="qctools.xml.gz"
    ))
    fixity: FixityConfig = field(default_factory=lambda: FixityConfig(
        check_fixity=False,
        validate_stream_fixity=False,
        embed_stream_fixity=False, 
        output_fixity=False,
        overwrite_stream_fixity=False,
        checksum_algorithm="md5",
        stream_hash_algorithm="md5"
    ))
    tools: ToolsConfig = field(default_factory=lambda: ToolsConfig(
        exiftool=BasicToolConfig(check_tool=False, run_tool=False),
        ffprobe=BasicToolConfig(check_tool=False, run_tool=False),
        mediaconch=MediaConchConfig(mediaconch_policy="", run_mediaconch=False),
        mediainfo=BasicToolConfig(check_tool=False, run_tool=False),
        mediatrace=BasicToolConfig(check_tool=False, run_tool=False),
        qctools=QCToolsConfig(run_tool=False),
        qct_parse=QCTParseToolConfig(
            run_tool=False,
            barsDetection=False,
            evaluateBars=False,
            thumbExport=False,
            audio_analysis=False,
            detect_clamped_levels=False
        ),
        mkvalidator=BasicToolConfig(check_tool=False, run_tool=False),
        clams_detection=ClamsDetectionConfig()
    ))
    video_file_extension: str = "mkv"

@dataclass
class ChecksProfilesConfig:
    """Container for custom checks profiles"""
    custom_profiles: Dict[str, ChecksProfile] = field(default_factory=dict)

@dataclass
class ExiftoolProfile:
    """Profile for exiftool expected values configuration"""
    FileType: str
    FileTypeExtension: str
    MIMEType: str
    VideoFrameRate: str
    ImageWidth: str
    ImageHeight: str
    VideoScanType: str
    DisplayWidth: str
    DisplayHeight: str
    DisplayUnit: str
    CodecID: List[str]
    AudioChannels: str
    AudioSampleRate: str
    AudioBitsPerSample: str

@dataclass
class ExiftoolConfig:
    """Container for exiftool profiles"""
    exiftool_profiles: Dict[str, ExiftoolProfile] = field(default_factory=dict)

@dataclass
class MediainfoProfile:
    """
    Profile for MediaInfo expected values configuration.
    
    Uses nested structure matching MediaInfo's General/Video/Audio sections.
    Reuses the existing MediainfoGeneralValues, MediainfoVideoValues, and
    MediainfoAudioValues dataclasses.
    
    ConfigManager compatibility:
        - MediainfoProfile has __dataclass_fields__, so _handle_dict will
          deserialize it from Dict[str, MediainfoProfile].
        - The nested general/video/audio fields are also dataclasses, so
          _process_field will recursively deserialize them.
    """
    general: MediainfoGeneralValues
    video: MediainfoVideoValues
    audio: MediainfoAudioValues


@dataclass
class MediainfoConfig:
    """
    Container for MediaInfo profiles.
    
    Loaded via: config_mgr.get_config('mediainfo', MediainfoConfig)
    Requires bundled file: config/mediainfo_config.json
    
    ConfigManager compatibility:
        - Dict[str, MediainfoProfile] value type has __dataclass_fields__,
          so _handle_dict will auto-deserialize each profile entry.
    """
    mediainfo_profiles: Dict[str, MediainfoProfile] = field(default_factory=dict)

@dataclass
class FfprobeProfile:
    """
    Profile for FFprobe expected values configuration.
    
    Uses nested structure matching FFprobe's video_stream/audio_stream/format
    sections. Reuses the existing FFmpegVideoStream, FFmpegAudioStream, and
    FFmpegFormat dataclasses.
    
    ConfigManager compatibility:
        - FfprobeProfile has __dataclass_fields__, so _handle_dict will
          deserialize it from Dict[str, FfprobeProfile].
        - The nested video_stream/audio_stream/format fields are also
          dataclasses, so _process_field will recursively deserialize them.
    """
    video_stream: FFmpegVideoStream
    audio_stream: FFmpegAudioStream
    format: FFmpegFormat


@dataclass
class FfprobeConfig:
    """
    Container for FFprobe profiles.
    
    Loaded via: config_mgr.get_config('ffprobe', FfprobeConfig)
    Requires bundled file: config/ffprobe_config.json
    
    ConfigManager compatibility:
        - Dict[str, FfprobeProfile] value type has __dataclass_fields__,
          so _handle_dict will auto-deserialize each profile entry.
    """
    ffprobe_profiles: Dict[str, FfprobeProfile] = field(default_factory=dict)