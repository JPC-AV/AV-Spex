#!/usr/bin/env python
# -*- coding: utf-8 -*-

# The majority of this code is derived from the open source project qct-parse
# which is licensed under the GNU Version 3 License. You may obtain a copy of the license at: https://github.com/FutureDays/qct-parse/blob/master/LICENSE
# Original code is here: https://github.com/FutureDays/qct-parse  

# The original code from the qct-parse was written by Brendan Coates and Morgan Morel as part of the 2016 AMIA "Hack Day"
# Summary of that event here: https://wiki.curatecamp.org/index.php/Association_of_Moving_Image_Archivists_%26_Digital_Library_Federation_Hack_Day_2016

import gzip
import math
import os
import subprocess
import shutil
import sys
import re
import operator
import collections      # for circular buffer
import statistics       # for median of bars values
import csv
import datetime as dt
import io
from dataclasses import asdict, dataclass, field
from collections import defaultdict

import numpy as np

from AV_Spex.utils.log_setup import logger, report_ffmpeg_stderr
from AV_Spex.utils import ffprobe_probe
from AV_Spex.utils.config_setup import ChecksConfig, SpexConfig
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.checks.tone_leak_check import analyzeToneLeak
from AV_Spex.checks.audio_stream_stats import audio_stats_path, generate_audio_stats_sidecar

config_mgr = ConfigManager()

def load_etree():
    """Helper function to load lxml.etree with error handling"""
    try:
        from lxml import etree
        return etree
    except ImportError as e:
        logger.critical(f"Error importing lxml.etree: {e}")
        return None


def safe_gzip_open_with_encoding_fallback(file_path):
    """
    Opens a gzipped file with encoding fallback handling.
    Returns the raw bytes with encoding information for logging.
    
    Parameters:
        file_path (str): Path to the .gz file
        
    Returns:
        tuple: (raw_bytes, encoding_used) or (None, None) if failed
    """
    encodings_to_try = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
    
    try:
        # Read the raw bytes first
        with gzip.open(file_path, 'rb') as gz_file:
            raw_content = gz_file.read()
    except Exception as e:
        logger.error(f"Error reading gzipped file {file_path}: {e}\n")
        return None, None
    
    # Try to decode with different encodings to find the right one
    for encoding in encodings_to_try:
        try:
            # Test if we can decode successfully
            decoded_content = raw_content.decode(encoding)
            # logger.debug(f"Successfully decoded {file_path} using {encoding} encoding\n")
            return raw_content, encoding
        except UnicodeDecodeError:
            continue
    
    # If all encodings fail, try with error handling
    try:
        decoded_content = raw_content.decode('utf-8', errors='replace')
        logger.warning(f"Used utf-8 with error replacement for {file_path}\n")
        return raw_content, 'utf-8-replace'
    except Exception as e:
        logger.critical(f"Failed to decode {file_path} with any encoding method: {e}\n")
        return None, None


def safe_gzip_iterparse(file_path, etree_module):
    """
    Safely parse gzipped XML with encoding fallback.
    
    Parameters:
        file_path (str): Path to the .gz file
        etree_module: The lxml.etree module
        
    Returns:
        iterator: XML parser iterator or None if failed
    """
    raw_content, encoding_used = safe_gzip_open_with_encoding_fallback(file_path)
    
    if raw_content is None:
        return None
    
    try:
        # Create a BytesIO object from the raw content
        # lxml.etree.iterparse expects a file-like object that returns bytes
        bytes_io = io.BytesIO(raw_content)
        
        # Create the iterparse iterator
        parser_iter = etree_module.iterparse(bytes_io, events=('end',), tag='frame')
        return parser_iter
        
    except Exception as e:
        logger.error(f"Error creating XML parser for {file_path}: {e}")
        return None


# Dictionary to map the string to the corresponding operator function
operator_mapping = {
    'lt': operator.lt,
    'gt': operator.gt,
}

def getFullTagList():
    """Heler function for retrieving tag list"""
    spex_config = config_mgr.get_config('spex', SpexConfig)
    # init variable for config list of QCTools tags
    fullTagList = asdict(spex_config.qct_parse_values.fullTagList)
    return fullTagList

# Creates timestamp for pkt_dts_time
def dts2ts(frame_pkt_dts_time):
    """
    Converts a time in seconds to a formatted time string in HH:MM:SS.ssss format.

    Parameters:
        frame_pkt_dts_time (str): The time in seconds as a string.

    Returns:
        str: The formatted time string.
    """

    seconds = float(frame_pkt_dts_time)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours < 10:
        hours = "0" + str(int(hours))
    else:
        hours = str(int(hours))  
    if minutes < 10:
        minutes = "0" + str(int(minutes))
    else:
        minutes = str(int(minutes))
    secondsStr = str(round(seconds, 4))
    if int(seconds) < 10:
        secondsStr = "0" + secondsStr
    else:
        seconds = str(minutes)
    while len(secondsStr) < 7:
        secondsStr = secondsStr + "0"
    timeStampString = hours + ":" + minutes + ":" + secondsStr
    return timeStampString


# finds stuff over/under threshold
def threshFinder(qct_parse, video_path, inFrame, startObj, pkt, tag, over, thumbPath, thumbDelay, thumbExportDelay, profile_name, failureInfo):
    """
    Compares tagValue in frameDict (from qctools.xml.gz) with threshold from config

    Parameters:
        qct_parse (dict): qct-parse dictionary from command_config.yaml 
        video_path (file): Path to the video file.
        inFrame (dict): The most recent frameDict in framesList
        startObj (qctools.xml.gz): Starting object or reference, used in logging or naming.
        pkt (str): The attribute key used to extract timestamps from <frame> tag in qctools.xml.gz.
        tag (str): Attribute tag from <frame> tag in qctools.xml.gz, is checked against the threshold.
        over (float): The threshold value to compare against, from config
        comp_op (callable): The comparison operator function (e.g., operator.lt, operator.gt).
        thumbPath (str): Path where thumbnails are saved.
        thumbDelay (int): Current delay count between thumbnails.
        thumbExportDelay (int): Required delay count between exporting thumbnails.
        profile_name (str): The name of the profile being checked against, used in naming thumbnail images
        failureInfo (dict): Dictionary that stores tag, tagValue and threshold value (over) for each failed timestamp

    Returns:
        tuple: (bool indicating if threshold was met, updated thumbDelay, updated failureInfo dictionary)
    """

    tagValue = float(inFrame[tag])
    frame_pkt_dts_time = inFrame[pkt]

    if "MIN" in tag or "LOW" in tag:
        comparision = operator.lt
    else:
        comparision = operator.gt
	
    if comparision(float(tagValue), float(over)): # if the attribute is over usr set threshold
        timeStampString = dts2ts(frame_pkt_dts_time)
        # Store failure information in the dictionary (update the existing dictionary, not create a new one)
        if timeStampString not in failureInfo:  # If timestamp not in dict, initialize an empty list
            failureInfo[timeStampString] = []

        failureInfo[timeStampString].append({  # Add failure details to the list
            'tag': tag,
            'tagValue': tagValue,
            'over': over
        })

        # Remove the thumbnail generation - just reset thumbDelay if conditions met
        if qct_parse['thumbExport'] and (thumbDelay > int(thumbExportDelay)):
            # Previously generated thumbnail here, now just reset the delay counter
            thumbDelay = 0
        
        return True, thumbDelay, failureInfo # return true because it was over and thumbDelay
    else:
        return False, thumbDelay, failureInfo # return false because it was NOT over and thumbDelay


#  print thumbnail images of overs/unders        
def printThumb(video_path, tag, profile_name, startObj, thumbPath, tagValue, timeStampString):
    """
    Exports a thumbnail image for a specific frame 

    Parameters:
        video_path (str): Path to the video file.
        tag (str): Attribute tag of the frame, used for naming the thumbnail.
        startObj
    """
    if os.path.isfile(video_path):
        video_basename = os.path.basename(video_path)
        video_id = os.path.splitext(video_basename)[0]
        outputFramePath = os.path.join(thumbPath, video_id + "." + profile_name + "." + tag + "." + str(tagValue) + "." + timeStampString + ".png")
        ffoutputFramePath = outputFramePath.replace(":", ".")
        # for windows we gotta see if that first : for the drive has been replaced by a dot and put it back
        match = ''
        match = re.search(r"[A-Z]\.\/", ffoutputFramePath) # matches pattern R./ which should be R:/ on windows
        if match:
            ffoutputFramePath = ffoutputFramePath.replace(".", ":", 1) # replace first instance of "." in string ffoutputFramePath
        if tag == "TOUT":
            ffmpegString = "ffmpeg -ss " + timeStampString + ' -i "' + video_path +  '" -vf signalstats=out=tout:color=yellow -vframes 1 -s 720x486 -y "' + ffoutputFramePath + '"' # Hardcoded output frame size to 720x486 for now, need to infer from input eventually
        elif tag == "VREP":
            ffmpegString = "ffmpeg -ss " + timeStampString + ' -i "' + video_path +  '" -vf signalstats=out=vrep:color=pink -vframes 1 -s 720x486 -y "' + ffoutputFramePath + '"' # Hardcoded output frame size to 720x486 for now, need to infer from input eventually
        else:
            ffmpegString = "ffmpeg -ss " + timeStampString + ' -i "' + video_path +  '" -vf signalstats=out=brng:color=cyan -vframes 1 -s 720x486 -y "' + ffoutputFramePath + '"' # Hardcoded output frame size to 720x486 for now, need to infer from input eventually
        # Removing logging statement for now - too much clutter in output
        # logger.warning(f"Exporting thumbnail image of {video_id} to {os.path.basename(ffoutputFramePath)}\n")
        output = subprocess.Popen(ffmpegString, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    else:
        logger.critical("Input video file not found when attempting to create thumbnail for report. Ensure video file is in the '_qc_metadata' directory as the QCTools report and report file name contains video file extension.")
        exit()
    return


# detect bars    
def detectBars(startObj, pkt, durationStart, durationEnd, framesList, buffSize, bit_depth_10, signals=None, total_duration=None, search_start_time=0.0, search_end_time=None, relaxed=False):
    """
    Detects color bars in a video by analyzing frames within a buffered window and logging the start and end times of the bars.

    This function iterates through the frames in a QCTools report, parses each frame, 
    and analyzes specific tags (YMAX, YMIN, YDIF) to detect the presence of color bars. 
    The detection checks a frame each time the buffer reaches the specified size (`buffSize`) and ends when the frame tags no longer match the expected bar values.

    Args:
    args (argparse.Namespace): Parsed command-line arguments.
    startObj (str): Path to the QCTools report file (.qctools.xml.gz)
    pkt (str): Key used to identify the packet timestamp (pkt_*ts_time) in the XML frames.
    durationStart (str): The timestamp when the bars start, initially an empty string.
    durationEnd (str): The timestamp when the bars end, initially an empty string.
    framesList (list): List of dictionaries storing the parsed frame data.
    buffSize (int): The size of the frame buffer to hold frames for analysis.

    Returns:
    tuple:
    float: The timestamp (`durationStart`) when the bars were first detected.
    float: The timestamp (`durationEnd`) when the bars were last detected.

    Behavior:
    - Parses the input XML file frame by frame.
    - Each frame's timestamp (`pkt_*ts_time`) and key-value pairs are stored in a dictionary (`frameDict`).
    - Once the buffer reaches the specified size (`buffSize`), it checks the middle frame's attributes:
    - Color bars are detected if `YMAX > 210`, `YMIN < 10`, and `YDIF < 3.0`.
    - Logs the start and end times of the bars and stops detection once the bars end.
    - Clears the memory of parsed elements to avoid excessive memory usage during parsing.

    Example log outputs:
    - "Bars start at [timestamp] ([formatted timestamp])"
    - "Bars ended at [timestamp] ([formatted timestamp])"
    """
    etree = load_etree()
    if etree is None:
        return "", "", None, None
    
    if bit_depth_10:
        YMAX_thresh = 800
        YMIN_thresh = 10
        YDIF_thresh = 8.0     # Allow up to 8.0 for 10-bit (your real bars have ~6.5)
        SATMAX_thresh = 250   # Key discriminator - real color bars have high saturation
        URANGE_min = 500      # Real color bars have large chroma ranges
        VRANGE_min = 500
    else:
        YMAX_thresh = 210
        YMIN_thresh = 10
        YDIF_thresh = 3.0
        SATMAX_thresh = 100   # Scaled for 8-bit
        URANGE_min = 150
        VRANGE_min = 150

    if relaxed:
        YMAX_thresh = int(YMAX_thresh * 0.75)
        YMIN_thresh = int(YMIN_thresh * 2)
        YDIF_thresh = YDIF_thresh * 2.0
        SATMAX_thresh = int(SATMAX_thresh * 0.4)

    barsStartString = None
    barsEndString = None
    consecutive_non_bar_frames = 0
    max_consecutive_non_bar_frames = 10  # Allow up to 10 consecutive bad frames before ending

    # Add time limit for searching (5 minutes = 300 seconds)
    search_time_limit = 300.0

    # Add tolerance for occasional frames that don't meet criteria
    consecutive_failures = 0
    failure_tolerance = 15 if not relaxed else 30

    # Confirmation window: require consecutive passing frames before confirming bars start
    bars_confirmation_count = 0
    bars_confirmation_threshold = 30 if not relaxed else 10
    bars_candidate_start = None

    # Use the safe parser with encoding fallback
    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for bars detection")
        return "", "", None, None

    try:
        bars_frame_count = 0
        bars_progress_interval = 500  # Emit progress every 500 frames
        bars_last_progress_pct = 0
        for event, elem in parser_iter: #iterparse the xml doc
            if elem.attrib['media_type'] == "video": #get just the video frames
                frame_pkt_dts_time = elem.attrib[pkt] #get the timestamps for the current frame we're looking at
                frame_time_float = float(frame_pkt_dts_time)

                if frame_time_float < search_start_time:
                    elem.clear()
                    continue

                # Emit progress during bars detection (maps into 0→5% range)
                bars_frame_count += 1
                if (signals and hasattr(signals, 'qctparse_progress') and
                    total_duration and bars_frame_count % bars_progress_interval == 0):
                    pct = int((frame_time_float / total_duration) * 5)
                    pct = min(5, max(0, pct))
                    if pct > bars_last_progress_pct:
                        signals.qctparse_progress.emit(pct)
                        bars_last_progress_pct = pct

                effective_search_limit = search_end_time if search_end_time is not None else search_time_limit
                if durationStart == "" and frame_time_float > effective_search_limit:
                    logger.debug(f"Stopped searching for color bars after reaching {effective_search_limit:.1f}s")
                    break
                if search_end_time is not None and durationStart != "" and frame_time_float > search_end_time:
                    if durationEnd not in ("", None):
                        barsEndString = dts2ts(str(durationEnd))
                    logger.debug(f"Reached end of search window at {search_end_time:.1f}s")
                    break
                
                frameDict = {}  #start an empty dict for the new frame
                frameDict[pkt] = frame_pkt_dts_time  #give the dict the timestamp, which we have now
                for t in list(elem):    #iterating through each attribute for each element
                    keySplit = t.attrib['key'].split(".")   #split the names by dots 
                    keyName = str(keySplit[-1])             #get just the last word for the key name
                    frameDict[keyName] = t.attrib['value']	#add each attribute to the frame dictionary
                framesList.append(frameDict)
                middleFrame = int(round(float(len(framesList))/2))	# get the middle index of the list
                if len(framesList) == buffSize:	# wait till the buffer is full to start detecting bars
                ## This is where the bars detection actually happens
                    # Check conditions - including saturation check
                    if (float(framesList[middleFrame]['YMAX']) > YMAX_thresh and 
                        float(framesList[middleFrame]['YMIN']) < YMIN_thresh and 
                        float(framesList[middleFrame]['YDIF']) < YDIF_thresh and
                        float(framesList[middleFrame].get('SATMAX', 0)) > SATMAX_thresh):  # Add saturation check
                        
                        consecutive_failures = 0  # Reset failure counter when bars are detected
                        
                        if durationStart == "":
                            # Track confirmation window before committing to bars start
                            bars_confirmation_count += 1
                            if bars_candidate_start is None:
                                bars_candidate_start = float(framesList[middleFrame][pkt])
                            
                            if bars_confirmation_count >= bars_confirmation_threshold:
                                durationStart = bars_candidate_start
                                barsStartString = dts2ts(str(bars_candidate_start))
                                logger.debug("Bars start at " + str(bars_candidate_start) + " (" + barsStartString + ")")
                        
                        if durationStart != "":
                            durationEnd = float(framesList[middleFrame][pkt])
                    else:
                        if durationStart == "":
                            # Reset confirmation window - unstable bars interrupted the run
                            bars_confirmation_count = 0
                            bars_candidate_start = None
                        else:
                            # Only count as failure if we've already confirmed bars start
                            consecutive_failures += 1

                            if consecutive_failures >= failure_tolerance:
                                if durationEnd - durationStart > 2:
                                    logger.debug("Bars ended at " + str(framesList[middleFrame][pkt]) + " (" + dts2ts(framesList[middleFrame][pkt]) + ")\n")
                                    barsEndString = dts2ts(framesList[middleFrame][pkt])
                                    break
                                else:
                                    # Confirmed run was too short to be real bars (e.g. a
                                    # bright saturated shot). Abandon it and resume searching —
                                    # otherwise the region can never terminate, and a stray
                                    # passing frame much later drags durationEnd across
                                    # content that isn't bars.
                                    logger.debug("Abandoning short bars candidate at " + str(durationStart) + " after " + str(consecutive_failures) + " consecutive non-bar frames\n")
                                    durationStart = ""
                                    durationEnd = ""
                                    barsStartString = None
                                    bars_confirmation_count = 0
                                    bars_candidate_start = None
                                    consecutive_failures = 0
                elem.clear() # we're done with that element so let's get it outta memory
    except Exception as e:
        logger.error(f"Error during bars detection parsing: {e}")
        
    return durationStart, durationEnd, barsStartString, barsEndString

def validateEntireVideoAsBars(startObj, pkt, durationStart, framesList, buffSize, bit_depth_10):
    """
    Validates if the entire video consists of color bars by checking TOUT, UMAX, UDIF, VMAX, and VDIF values.
    This is used when bars start is detected but no end is found.
    
    Parameters:
        startObj (str): Path to the QCTools report file (.qctools.xml.gz)
        pkt (str): The attribute key used to extract timestamps from <frame> tag in qctools.xml.gz.
        durationStart (float): Initial timestamp marking the start of detected bars.
        framesList (list): List of frameDict dictionaries
        buffSize (int): The size of the frame buffer
        bit_depth_10 (bool): Whether the video is 10-bit depth
    
    Returns:
        tuple: (is_entire_video_bars (bool), video_end_time (float or None))
               Returns (True, end_time) if entire video is bars, (False, None) otherwise
    """
    etree = load_etree()
    if etree is None:
        return False, None
    
    # Threshold for validation
    TOUT_MAX = 0.020  # TOUT should be below 0.020 for color bars
    
    # Thresholds for basic color bar characteristics
    if bit_depth_10:
        YMAX_thresh = 800
        YMIN_thresh = 10
        YDIF_thresh = 25
        SATMAX_thresh = 300
        UMAX_thresh = 1020  
        UDIF_thresh = 20   
        VMAX_thresh = 970  
        VDIF_thresh = 15   
    else:
        YMAX_thresh = 199
        YMIN_thresh = 3
        YDIF_thresh = 2
        SATMAX_thresh = 75
        UMAX_thresh = 255  
        UDIF_thresh = 5
        VMAX_thresh = 243   
        VDIF_thresh = 4
    
    logger.debug(f"No end duration found for color bars - validating if entire video is color bars using TOUT (threshold: {TOUT_MAX}), UMAX, UDIF, VMAX, VDIF")
    
    # Counters for validation
    total_frames_checked = 0
    frames_meeting_criteria = 0
    video_end_time = None
    
    # Use the safe parser with encoding fallback
    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for entire video validation")
        return False, None
    
    try:
        for event, elem in parser_iter:
            if elem.attrib['media_type'] == "video":
                frame_pkt_dts_time = elem.attrib[pkt]
                video_end_time = float(frame_pkt_dts_time)  # Update with each frame
                
                frameDict = {}
                frameDict[pkt] = frame_pkt_dts_time
                for t in list(elem):
                    keySplit = t.attrib['key'].split(".")
                    keyName = str(keySplit[-1])
                    frameDict[keyName] = t.attrib['value']
                
                framesList.append(frameDict)
                
                if len(framesList) == buffSize:
                    middleFrame = int(round(float(len(framesList))/2))
                    total_frames_checked += 1
                    
                    # Check if frame meets all color bar criteria
                    try:
                        tout_value = float(framesList[middleFrame].get('TOUT', 999))
                        ymax_value = float(framesList[middleFrame].get('YMAX', 0))
                        ymin_value = float(framesList[middleFrame].get('YMIN', 999))
                        ydif_value = float(framesList[middleFrame].get('YDIF', 999))
                        satmax_value = float(framesList[middleFrame].get('SATMAX', 0))
                        # New checks
                        umax_value = float(framesList[middleFrame].get('UMAX', 9999))
                        udif_value = float(framesList[middleFrame].get('UDIF', 9999))
                        vmax_value = float(framesList[middleFrame].get('VMAX', 9999))
                        vdif_value = float(framesList[middleFrame].get('VDIF', 9999))
                        
                        # Check TOUT, basic bar characteristics, and U/V values
                        if (tout_value <= TOUT_MAX and 
                            ymax_value > YMAX_thresh and 
                            ymin_value < YMIN_thresh and 
                            ydif_value < YDIF_thresh and
                            satmax_value > SATMAX_thresh and
                            umax_value < UMAX_thresh and
                            udif_value < UDIF_thresh and
                            vmax_value < VMAX_thresh and
                            vdif_value < VDIF_thresh):
                            frames_meeting_criteria += 1
                    except (ValueError, KeyError) as e:
                        logger.debug(f"Error checking frame values: {e}")
                        pass
                
                elem.clear()
    except Exception as e:
        logger.error(f"Error during entire video validation parsing: {e}")
        return False, None
    
    # Calculate percentage of frames meeting criteria
    if total_frames_checked > 0:
        percentage_valid = (frames_meeting_criteria / total_frames_checked) * 100
        logger.debug(f"Validation results: {frames_meeting_criteria}/{total_frames_checked} frames ({percentage_valid:.1f}%) meet color bar criteria")
        
        # If at least 95% of frames meet the criteria, consider entire video as bars
        if percentage_valid >= 95.0:
            logger.info(f"Confirmed: Entire video consists of color bars (TOUT <= {TOUT_MAX}, UMAX < {UMAX_thresh}, UDIF < {UDIF_thresh}, VMAX < {VMAX_thresh}, VDIF < {VDIF_thresh})")
            return True, video_end_time
        else:
            logger.debug(f"Only {percentage_valid:.1f}% of frames meet criteria - not confirming as entire video bars")
            return False, None
    else:
        logger.debug("No frames were checked during validation")
        return False, None
    

# Color bars keys that are not signal levels on the 0-1023 scale. They take part in
# the bars evaluation but are skipped by the detected-vs-SMPTE levels comparison.
BARS_NON_LEVEL_KEYS = ('BRNG',)

# Out-of-range pixel share of a correct SMPTE bars signal, measured from the
# ffmpeg-generated 10-bit reference the smpte_color_bars config values come from.
# Only a backstop for a missing/malformed config value — the config is the source
# of truth. See SmpteColorBars.BRNG in utils/config_setup.py.
SMPTE_BRNG_DEFAULT = 0.0118


def _get_smpte_brng_floor():
    """
    Return the configured SMPTE BRNG value, used as the floor under the bars-derived
    BRNG threshold (and as the threshold outright when no bars were detected).
    """
    spex_config = config_mgr.get_config('spex', SpexConfig)
    try:
        return float(getattr(spex_config.qct_parse_values.smpte_color_bars, 'BRNG', SMPTE_BRNG_DEFAULT))
    except (TypeError, ValueError):
        logger.warning(f"Malformed SMPTE BRNG value in spex config - using {SMPTE_BRNG_DEFAULT}\n")
        return SMPTE_BRNG_DEFAULT


def evalBars(startObj,pkt,durationStart,durationEnd,framesList,buffSize):
    """
    Find the median values for specific QCTools keys inside the duration of the color bars.

    The value returned for each key is the median across every measured frame in the
    detected bars region, rather than a single peak/trough frame. The median ignores
    transient outliers — most notably the frames where the bars "crash" in or out — so
    the returned values faithfully represent the steady color bars recorded on the tape.
    The frame buffer (buffSize) additionally delays the start of measurement, so the
    leading crash-in frames are excluded before the median is taken.

    BRNG is measured alongside the level tags but handled differently: it is the share
    of out-of-range pixels (0-1), so it keeps float precision instead of being rounded
    to the integer QCTools level scale, and its median is floored at the configured
    SMPTE BRNG value. The median makes a good per-tape reference — "content should not
    be further out of range than this tape's own bars" — and analog bars measure
    0.008-0.03 on the sample tapes. The floor keeps a bars region that measures below
    the SMPTE reference (0.0118 — correct bars are not out-of-range-free) from driving
    the threshold down toward zero, where every content frame would fail.

    Parameters:
        pkt (str): The attribute key used to extract timestamps from <frame> tag in qctools.xml.gz.
        durationStart (float): Initial timestamp marking the potential start of detected bars.
        durationEnd (float): Timestamp marking the end of detected bars.
        framesList (list): List of frameDict dictionaries

    Returns:
        maxBarsDict (dict): Returns dictionary of the median value of corresponding QCTools keys
    """

    etree = load_etree()
    if etree is None:
        return None

    # Define the keys for which you want to calculate the median
    keys_to_check = ['YMAX', 'YMIN', 'UMIN', 'UMAX', 'VMIN', 'VMAX', 'SATMAX', 'SATMIN', 'BRNG']
    # Collect every measured value per key so we can take the median across all
    # measured bars frames once parsing is complete.
    barsValues = {key_being_checked: [] for key_being_checked in keys_to_check}

    # Use the safe parser with encoding fallback
    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for bars evaluation")
        return None

    try:
        for event, elem in parser_iter: # iterparse the xml doc
            if elem.attrib['media_type'] == "video": # get just the video frames
                frame_pkt_dts_time = elem.attrib[pkt] # get the timestamps for the current frame we're looking at
                if frame_pkt_dts_time >= str(durationStart): 	# only work on frames that are after the start time   # only work on frames that are after the start time
                    if float(frame_pkt_dts_time) > durationEnd:        # only work on frames that are before the end time
                        break
                    frameDict = {}  # start an empty dict for the new frame
                    frameDict[pkt] = frame_pkt_dts_time  # give the dict the timestamp, which we have now
                    for t in list(elem):    # iterating through each attribute for each element
                        keySplit = t.attrib['key'].split(".")   # split the names by dots
                        keyName = str(keySplit[-1])             # get just the last word for the key name
                        frameDict[keyName] = t.attrib['value']	# add each attribute to the frame dictionary
                    framesList.append(frameDict)
                    if len(framesList) == buffSize:	# wait till the buffer is full to start measuring bars
                        ## This is where the bars measurement magic actually happens
                        for colorbar_key in keys_to_check:
                            if colorbar_key in frameDict:
                                # Collect the value so the region median can be
                                # computed once every frame has been visited.
                                barsValues[colorbar_key].append(float(frameDict[colorbar_key]))
    except Exception as e:
        logger.error(f"Error during bars evaluation parsing: {e}")

    # Compute the median per key, rounded to an integer to match the QCTools value
    # scale. Keys with no measured frames fall back to a permissive extreme so the
    # downstream threshold never rejects everything.
    smpte_brng_floor = _get_smpte_brng_floor()
    maxBarsDict = {}
    for colorbar_key in keys_to_check:
        if colorbar_key == "BRNG":
            # Fraction of out-of-range pixels: keep float precision (rounding to
            # int would zero it out) and never let the threshold fall below the
            # configured floor. Unmeasured bars fall back to the floor itself.
            if barsValues[colorbar_key]:
                measured_brng = round(statistics.median(barsValues[colorbar_key]), 5)
                maxBarsDict[colorbar_key] = max(measured_brng, smpte_brng_floor)
                if maxBarsDict[colorbar_key] != measured_brng:
                    logger.debug(
                        f"Measured bars BRNG median {measured_brng} is below the configured "
                        f"floor - using {smpte_brng_floor} as the BRNG threshold\n"
                    )
            else:
                maxBarsDict[colorbar_key] = smpte_brng_floor
        elif barsValues[colorbar_key]:
            maxBarsDict[colorbar_key] = int(round(statistics.median(barsValues[colorbar_key])))
        else:
            maxBarsDict[colorbar_key] = 0 if "MAX" in colorbar_key else 1023

    return maxBarsDict


def getCompFromConfig(qct_parse, profile, tag):
   """
   Determines the comparison operator based on profile and tag.

    Args:
        qct_parse (dict): qct-parse configuration.
        profile (dict): Profile data.
        tag (str): Tag to check.

    Returns:
        callable: Comparison operator (e.g., operator.lt, operator.gt).
   """
   
   spex_config = config_mgr.get_config('spex', SpexConfig)
   
   smpte_color_bars_keys = asdict(spex_config.qct_parse_values.smpte_color_bars).keys()

   if set(profile) == set(smpte_color_bars_keys):
       return operator.lt if "MIN" in tag else operator.gt

   raise ValueError(f"No matching comparison operator found for profile and tag: {profile}, {tag}")


def analyzeIt(qct_parse, video_path, profile, profile_name, startObj, pkt, durationStart, durationEnd, thumbPath, thumbDelay, thumbExportDelay, framesList, frameCount=0, overallFrameFail=0, adhoc_tag=False, check_cancelled=None, signals=None, total_duration=None, skip_regions=None):
    """
    Analyzes video frames from the QCTools report to detect threshold exceedances for specified tags or profiles and logs frame failures.

    This function iteratively parses video frames from a QCTools report (`.qctools.xml.gz`) and checks whether the frame attributes exceed user-defined thresholds 
    (either single tags or profiles). Threshold exceedances are logged, and frames can be flagged for further analysis. Optionally, thumbnails of failing frames can be generated.

    Args:
        args (argparse.Namespace): Parsed command-line arguments, including tag thresholds and options for profile, thumbnail export, etc.
        profile (dict): A dictionary of key-value pairs of tag names and their corresponding threshold values.
        startObj (str): Path to the QCTools report file (.qctools.xml.gz)
        pkt (str): Key used to identify the pkt_*ts_time in the XML frames.
        durationStart (float): The starting time for analyzing frames (in seconds).
        durationEnd (float): The ending time for analyzing frames (in seconds). Can be `None` to process until the end.
        thumbPath (str): Path to save the thumbnail images of frames exceeding thresholds.
        thumbDelay (int): Delay counter between consecutive thumbnail generations to prevent spamming.
        framesList (list): A circular buffer to hold dictionaries of parsed frame attributes.
        frameCount (int, optional): The total number of frames analyzed (defaults to 0).
        overallFrameFail (int, optional): A count of how many frames failed threshold checks across all tags (defaults to 0).
        skip_regions (list, optional): (start_seconds, end_seconds) spans to exclude from
            evaluation entirely — used by the color-bars evaluation to skip the detected
            bars regions themselves, so only content is scored against the bars-derived
            thresholds. Skipped frames are excluded from frameCount.

    Returns:
        tuple: 
            - kbeyond (dict): A dictionary where each tag is associated with a count of how many times its threshold was exceeded.
            - frameCount (int): The total number of frames analyzed.
            - overallFrameFail (int): The total number of frames that exceeded thresholds across all tags.
            - failureInfo (dict): Dictionary containing failure information.

    Behavior:
        - Iteratively parses the input XML file and analyzes frames after `durationStart` and before `durationEnd`.
        - Frames are stored in a circular buffer (`framesList`), and attributes (tags) are extracted into dictionaries.
        - For each frame, checks whether specified tags exceed user-defined thresholds (from `args.o`, `args.u`, or `profile`).
        - Logs threshold exceedances and updates the count of failed frames.
        - Optionally, generates thumbnails for frames that exceed thresholds, ensuring a delay between consecutive thumbnails.

    Example usage:
        - Analyzing frames using a single tag threshold: `analyzeIt(args, {}, startObj, pkt, durationStart, durationEnd, thumbPath, thumbDelay, framesList)`
        - Analyzing frames using a profile: `analyzeIt(args, profile, startObj, pkt, durationStart, durationEnd, thumbPath, thumbDelay, framesList)`
    """
    etree = load_etree()
    if etree is None:
        return {}, 0, 0, {}
    
    kbeyond = {} # init a dict for each key which we'll use to track how often a given key is over
    fots = "" # init frame over threshold to avoid counting the same frame more than once in the overallFrameFail count
    failureInfo = {}  # Initialize a new dictionary to store failure information
    for k,v in profile.items(): 
        kbeyond[k] = 0

    # Progress emission setup: emit every ~1000 frames to avoid overhead
    # Maps frame timestamp against total_duration into the 5→36% range
    # (later stages: clamped levels 36→64, audio analysis 64→99)
    progress_emit_interval = 1000
    last_progress_pct = 5

    # Use the safe parser with encoding fallback
    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for analysis")
        return {}, 0, 0, {}

    try:
        for event, elem in parser_iter: #iterparse the xml doc
            if elem.attrib['media_type'] == "video": 	#get just the video frames
                frameCount = frameCount + 1
                frame_pkt_dts_time = elem.attrib[pkt] 	#get the timestamps for the current frame we're looking at
                
                # Emit progress periodically
                if (signals and hasattr(signals, 'qctparse_progress') and
                    total_duration and frameCount % progress_emit_interval == 0):
                    pct = 5 + int((float(frame_pkt_dts_time) / total_duration) * 31)
                    pct = min(36, max(5, pct))
                    if pct > last_progress_pct:
                        signals.qctparse_progress.emit(pct)
                        last_progress_pct = pct
                
                if frame_pkt_dts_time >= str(durationStart): 	#only work on frames that are after the start time
                    if check_cancelled():
                        return kbeyond, frameCount, overallFrameFail, failureInfo
                    if durationEnd:
                        if float(frame_pkt_dts_time) > durationEnd:		#only work on frames that are before the end time
                            print("started at " + str(durationStart) + " seconds and stopped at " + str(frame_pkt_dts_time) + " seconds (" + dts2ts(frame_pkt_dts_time) + ") or " + str(frameCount) + " frames!")
                            break
                    if skip_regions:
                        frame_time = float(frame_pkt_dts_time)
                        if any(rs <= frame_time <= re for rs, re in skip_regions):
                            frameCount = frameCount - 1  # keep percentage denominators content-only
                            elem.clear()
                            continue
                    frameDict = {}  								#start an empty dict for the new frame
                    frameDict[pkt] = frame_pkt_dts_time  			#make a key for the timestamp, which we have now
                    for t in list(elem):    						#iterating through each attribute for each element
                        keySplit = t.attrib['key'].split(".")   	#split the names by dots 
                        keyName = str(keySplit[-1])             	#get just the last word for the key name
                        if len(keyName) == 1:						#if it's psnr or mse, keyName is gonna be a single char
                            keyName = '.'.join(keySplit[-2:])		#full attribute made by combining last 2 parts of split with a period in btw
                        frameDict[keyName] = t.attrib['value']		#add each attribute to the frame dictionary
                    framesList.append(frameDict)					#add this dict to our circular buffer
                    # Now we can parse the frame data from the buffer!	
                    for k,v in profile.items():
                        tag = k
                        over = float(v)
                        # ACTUALLY DO THE THING ONCE FOR EACH TAG
                        frameOver, thumbDelay, failureInfo = threshFinder(qct_parse, video_path, framesList[-1], startObj, pkt, tag, over, thumbPath, thumbDelay, thumbExportDelay, profile_name, failureInfo)
                        if frameOver is True:
                            kbeyond[k] = kbeyond[k] + 1 # note the over in the key over dict
                            if not frame_pkt_dts_time in fots: # make sure that we only count each over frame once
                                overallFrameFail = overallFrameFail + 1
                                fots = frame_pkt_dts_time # set it again so we don't dupe
                    thumbDelay = thumbDelay + 1				
                elem.clear() #we're done with that element so let's get it outta memory
    except Exception as e:
        logger.error(f"Error during analysis parsing: {e}")

    return kbeyond, frameCount, overallFrameFail, failureInfo


def print_color_bar_values(video_id, smpte_color_bars, maxBarsDict, colorbars_values_output):
    """
    Writes color bar values to a CSV file.

    Compares SMPTE color bar values with those extracted from a video using QCTools.
    The output CSV includes the attribute name, the expected SMPTE value, and the value detected in the video.

    BRNG is deliberately omitted: it is a share of out-of-range pixels (0-1), not a
    signal level on the 0-1023 scale the other keys sit on, so it would plot as an
    invisible bar in the report's detected-vs-SMPTE graph. BRNG participates in the
    bars *evaluation* (and therefore the failure timeline) instead.

    Args:
        video_id (str): Identifier for the video being analyzed.
        smpte_color_bars (dict): Dictionary of expected SMPTE color bar values, from config.yaml
        maxBarsDict (dict): Dictionary of color bar values extracted from the video.
        colorbars_values_output (str): Path to the output CSV file.
    """

    with open(colorbars_values_output, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)

        # Write the header
        writer.writerow(["QCTools Fields", "SMPTE Colorbars", f"{video_id} Colorbars"])

        # Write the data
        for key in smpte_color_bars:
            if key in BARS_NON_LEVEL_KEYS:
                continue
            smpte_value = smpte_color_bars.get(key, "")
            maxbars_value = maxBarsDict.get(key, "")
            writer.writerow([key, smpte_value, maxbars_value])


def printresults(profile, kbeyond, frameCount, overallFrameFail, qctools_check_output):
    """
    Writes the analyzeIt results into a summary file, detailing the count and percentage of frames that exceeded the thresholds.

    Parameters:
        kbeyond (dict): Dictionary mapping tags to the count of frames exceeding the thresholds.
        frameCount (int): Total number of frames analyzed.
        overallFrameFail (int): Total number of frames with at least one threshold exceedance.
        qctools_check_output (str): File path to write the output summary.

    Returns:
        None
    """

    
    spex_config = config_mgr.get_config('spex', SpexConfig)
    fullTagList = getFullTagList()
    
    def format_percentage(value):
        percent = value * 100
        if percent == 100:
            return "100"
        elif percent == 0:
            return "0"
        elif percent < 0.01:
            return "0"
        else:
            return f"{percent:.2f}"

    color_bar_dict = asdict(spex_config.qct_parse_values.smpte_color_bars)
    color_bar_keys = color_bar_dict.keys()

    with open(qctools_check_output, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)

        writer.writerow(["**************************"])

        if profile == fullTagList:
            writer.writerow(["qct-parse evaluation of user specified tags summary"])
        elif set(profile.keys()) == set(color_bar_keys):
            writer.writerow(["qct-parse color bars evaluation summary"])
        else:
            writer.writerow(["qct-parse profile results summary"])

        if frameCount == 0:
            writer.writerow(["TotalFrames", "0"])
            return

        writer.writerow(["TotalFrames", frameCount])
        writer.writerow(["Tag", "Number of failed frames", "Percentage of failed frames"])

        for tag, count in kbeyond.items():
            percentOverString = format_percentage(count / frameCount)
            writer.writerow([tag, count, percentOverString])

        percentOverallString = format_percentage(overallFrameFail / frameCount)
        writer.writerow(["Total", overallFrameFail, percentOverallString])


def print_color_bar_keys(qctools_colorbars_values_output, profile, color_bar_keys):
    """
    Writes color bar keys and their threshold values to a CSV file.

    If the provided `profile` keys match the expected `color_bar_keys`,
    the function writes a header indicating the thresholds are based on the median QCTools filter values.
    Then, it writes each key and its corresponding threshold value from the `profile`.

    Args:
    qctools_colorbars_values_output (str): Path to the output CSV file.
    profile (dict): Dictionary containing color bar keys and their threshold values.
    color_bar_keys (list): List of expected color bar keys.
    """

    with open(qctools_colorbars_values_output, 'w') as csvfile:
        writer = csv.writer(csvfile)
        if set(profile.keys()) == set(color_bar_keys):
            writer.writerow(["The thresholds defined by the median values of QCTools filters in the identified color bars are:"])
            for key, value in profile.items():
                writer.writerow([key, value])


def print_timestamps(qctools_timestamp_output, summarized_timestamps, descriptor):
    """
    Writes timestamps of frames with failures to a CSV file.

    If `summarized_timestamps` is not empty, it writes a header indicating the timestamps correspond to frames
    with at least one failure during the qct-parse process, along with the provided `descriptor`.
    Then, for each start and end timestamp pair in `summarized_timestamps`, it writes either a single timestamp 
    (if start and end are the same) or a range of timestamps in the format "HH:MM:SS.mmm, HH:MM:SS.mmm".

    Args:
        qctools_timestamp_output (str): Path to the output CSV file.
        summarized_timestamps (list of tuples): List of (start, end) timestamp pairs.
        descriptor (str): Description of the analysis or filter applied.
    """

    with open(qctools_timestamp_output, 'w') as csvfile:
        writer = csv.writer(csvfile)
        if summarized_timestamps:
            writer.writerow([f"Times stamps of frames with at least one fail during qct-parse {descriptor}"])
        for start, end in summarized_timestamps:
            if start == end:
                writer.writerow([start.strftime("%H:%M:%S.%f")[:-3]])
            else:
                writer.writerow([f"{start.strftime('%H:%M:%S.%f')[:-3]}, {end.strftime('%H:%M:%S.%f')[:-3]}"])


def print_bars_durations(qctools_check_output, barsStartString=None, barsEndString=None, regions=None):
    """
    Writes color bar duration information to a CSV file.

    When *regions* is provided (a list of (label, start_ts, end_ts) tuples),
    the 3-column labeled format is written — one row per detected region.
    When *regions* is None, falls back to the legacy 2-column single-row format.

    Args:
        qctools_check_output (str): Path to the output CSV file.
        barsStartString (str or None): Start timestamp (legacy mode).
        barsEndString (str or None): End timestamp (legacy mode).
        regions (list or None): List of (label, start_ts_string, end_ts_string).
    """
    with open(qctools_check_output, 'w') as csvfile:
        writer = csv.writer(csvfile)
        if regions is not None:
            if regions:
                writer.writerow(["qct-parse color bars found:"])
                for label, start_str, end_str in regions:
                    writer.writerow([label, start_str, end_str])
            else:
                writer.writerow(["qct-parse found no color bars"])
        elif barsStartString and barsEndString:
            writer.writerow(["qct-parse color bars found:"])
            writer.writerow([barsStartString, barsEndString])
        else:
            writer.writerow(["qct-parse found no color bars"])


# blatant copy paste from https://stackoverflow.com/questions/13852700/create-file-but-if-name-exists-add-number
def uniquify(path):
    if os.path.isdir(path):
        original_path = path.rstrip(os.sep)  # Remove trailing separator if it exists
        counter = 1
        while os.path.exists(path):
            path = original_path + " (" + str(counter) + ")"
            counter += 1
        return path
    else:
        filename, extension = os.path.splitext(path)
        counter = 1
        while os.path.exists(path):
            path = filename + " (" + str(counter) + ")" + extension
            counter += 1
        return path


def archiveThumbs(thumbPath):
    """
    Archives thumbnail images in a dated subdirectory.

    Checks if the specified `thumbPath` contains any files. If so, it creates a new subdirectory 
    named `archivedThumbs_YYYY_MM_DD` (where YYYY_MM_DD is the creation date of `thumbPath`) 
    and moves all files (except '.DS_Store') from `thumbPath` into this archive directory.
    If a file with the same name already exists in the archive, it's renamed to ensure uniqueness.

    Args:
        thumbPath (str): The path to the directory containing thumbnail images.

    Returns:
        str or None: The path to the newly created archive directory if thumbnails were archived, 
                        otherwise None if `thumbPath` was empty.
    """

    # Check if thumbPath contains any files
    has_files = False
    for entry in os.scandir(thumbPath):
        if entry.is_file():
            has_files = True
            break

    # If thumbPath contains files, create the archive directory
    if has_files:
        # Get the creation time of the thumbPath directory
        creation_time = os.path.getctime(thumbPath)
        creation_date = dt.datetime.fromtimestamp(creation_time)

        # Format the date as YYYY_MM_DD
        date_str = creation_date.strftime('%Y_%m_%d')

        # Create the new directory name
        archive_dir = os.path.join(thumbPath, f'archivedThumbs_{date_str}')

        if os.path.exists(archive_dir):
            archive_dir = archive_dir
        else:
            # Create the archive directory
            os.makedirs(archive_dir)

        # Move all files from thumbPath to archive_dir
        for entry in os.scandir(thumbPath):
            # if an item in the ThumbExports directory is a file, and is no .DS_Store, then:
            if entry.is_file() and entry.name != '.DS_Store':
                # define the new path of the thumbnail, once it has been moved to archive_dir
                entry_archive_path = os.path.join(archive_dir, os.path.basename(entry))
                # But if the new path for that thumbnail is already taken:
                if os.path.exists(entry_archive_path):
                    # Create a unique path for the archived thumb (original name plus sequential number in parentheses (1), (2), etc.)
                    unique_file_path = uniquify(entry_archive_path)
                    # Rename the existing thumb to match the unique path (also moves the file)
                    os.rename(entry, unique_file_path)
                else:
                    shutil.move(entry, archive_dir)

        return archive_dir
    else:
        return None


def save_failures_to_csv(failureInfo, failure_csv_path):
    """Saves the failure information to a CSV file.

    Args:
        failureInfo (dict): A dictionary where keys are timestamps and values are lists of failure details.
        failure_csv_path (str, optional): The path to the CSV file. Defaults to 'failures.csv'.
    """
    with open(failure_csv_path, 'w', newline='') as csvfile:
        fieldnames = ['Timestamp', 'Tag', 'Tag Value', 'Threshold']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for timestamp, info_list in failureInfo.items():
            for info in info_list:
                writer.writerow({'Timestamp': timestamp, 'Tag': info['tag'], 'Tag Value': info['tagValue'], 'Threshold': info['over']})

def extract_report_mkv(startObj, qctools_output_path):
    etree = load_etree()
    if etree is None:
        return None
    
    report_file_output = qctools_output_path.replace(".qctools.mkv", ".qctools.xml.gz")

    # If previous report exists, remove it
    if os.path.isfile(report_file_output):
        os.remove(report_file_output)

    # Run ffmpeg command to extract xml.gz report
    # stderr is captured (rather than silenced with -loglevel panic) so a failed
    # extraction is diagnosable from the user's log - the reason is usually
    # "no attachment stream in the mkv" or "cannot write to the report folder".
    full_command = [
        'ffmpeg', 
        '-hide_banner', 
        '-loglevel', 'error', 
        '-dump_attachment:t:0', report_file_output, 
        '-i', qctools_output_path
    ]
    logger.info(f'Extracting qctools.xml.gz report from {os.path.basename(qctools_output_path)}\n')
    logger.debug(f'Running command: {" ".join(full_command)}\n')
    try:
        result = subprocess.run(full_command, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as e:
        logger.critical(f'Unable to run ffmpeg to extract XML from QCTools mkv report file: {e}\n')
        return None

    ffmpeg_stderr = (result.stderr or '').strip()

    if os.path.isfile(report_file_output):
        if ffmpeg_stderr:
            logger.debug(f'ffmpeg reported while extracting the QCTools report:\n{ffmpeg_stderr}\n')
        startObj = report_file_output
    else:
        logger.critical(f'Unable to extract XML from QCTools mkv report file\n')
        if ffmpeg_stderr:
            logger.error(f'ffmpeg error:\n{ffmpeg_stderr}\n')
        else:
            logger.error(
                f'ffmpeg exited with code {result.returncode} and no error output. '
                f'The QCTools mkv may have no attachment stream, or '
                f'{os.path.dirname(report_file_output)} may not be writable.\n'
            )
        startObj = None
    
    return startObj
    

def detectBitdepth(startObj,pkt,framesList,buffSize):
    etree = load_etree()
    if etree is None:
        return False

    bit_depth_10 = False
    
    # Use the safe parser with encoding fallback
    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for bit depth detection")
        return False

    try:
        for event, elem in parser_iter: # iterparse the xml doc
            if elem.attrib['media_type'] == "video": # get just the video frames
                frame_pkt_dts_time = elem.attrib[pkt] # get the timestamps for the current frame we're looking at
                frameDict = {}  # start an empty dict for the new frame
                frameDict[pkt] = frame_pkt_dts_time  # give the dict the timestamp, which we have now
                for t in list(elem):    # iterating through each attribute for each element
                    keySplit = t.attrib['key'].split(".")   # split the names by dots 
                    keyName = str(keySplit[-1])             # get just the last word for the key name
                    frameDict[keyName] = t.attrib['value']	# add each attribute to the frame dictionary
                framesList.append(frameDict)
                middleFrame = int(round(float(len(framesList))/2))	# i hate this calculation, but it gets us the middle index of the list as an integer
                if len(framesList) == buffSize:	# wait till the buffer is full to start detecting bars
                    ## This is where the bars detection magic actually happens
                    bufferRange = list(range(0, buffSize))
                    if float(framesList[middleFrame]['YMAX']) > 250:
                        bit_depth_10 = True
                        break
                elem.clear() # we're done with that element so let's get it outta memory
    except Exception as e:
        logger.error(f"Error during bit depth detection parsing: {e}")

    return bit_depth_10


AUDIO_CLIPPING_THRESHOLD_DB = -0.5
SILENCE_THRESHOLD_DB = -60.0

# ---------------------------------------------------------------------------
# Audible timecode detection thresholds
# ---------------------------------------------------------------------------

# R128 thresholds (derived from analysis of known TC files)
_TC_R128_WINDOW_SEC = 30          # rolling window size in seconds
_TC_R128_MIN_WINDOWS = 6          # minimum consecutive windows to confirm TC (~3 min)

# Criterion A: stable mix at TC level. r128.M is a mix-loudness measurement,
# so this fires for both "both channels carry TC" and "one channel TC + one
# near-silent" — the per-channel split must come from astats.
_TC_R128_A_M_STDEV_MAX = 2.0
_TC_R128_A_M_MEAN_MIN = -25.0
# LRA is cumulative back to file start, so it stays inflated through the first
# 30-60s after a silence-to-TC transition. The stronger discriminators are
# M_stdev and M_mean — LRA only needs to bound out program audio (LRA >> 10).
_TC_R128_A_LRA_MEDIAN_MAX = 8.5

# Criterion B: TC + silence (one channel TC, other near-silent)
_TC_R128_B_LRA_HIGH_MIN = -30.0
_TC_R128_B_LRA_HIGH_MAX = -10.0
_TC_R128_B_LRA_HIGH_STDEV_MAX = 2.0
_TC_R128_B_M_I_DIFF_MIN = 20.0
_TC_R128_B_LRA_MIN = 13.0

# Criterion C: TC + program audio
_TC_R128_C_MS_DIFF_MEDIAN_MIN = 1.5
_TC_R128_C_M_STDEV_MIN = 8.0
_TC_R128_C_LRA_HIGH_MIN = -30.0
_TC_R128_C_LRA_HIGH_MAX = -10.0
_TC_R128_C_LRA_HIGH_STDEV_MAX = 3.0
_TC_R128_C_LRA_HIGH_UPPER = -24.0
_TC_R128_C_M_MIN = -60.0

# astats thresholds (per-channel detection)
# Calibrated against three audible-TC samples (164811/246492/389332) and two
# non-TC negatives: a silence pad (JPC_AV_00011 1:06–3:08) and a 1 kHz bars+tone
# leader (JPC_AV_01663 0–45 s). Each gate independently rejects at least one
# non-TC regime — TC is "stable mid-level biphase mark", so means and *stdevs*
# both matter.
_TC_ASTATS_WINDOW_SEC = 30.0     # rolling window size in seconds (matches R128)
_TC_ASTATS_MIN_WINDOWS = 2       # minimum consecutive windows to confirm TC

# Per-channel TC indicators in astats — mean bands
_TC_ASTATS_RMS_LEVEL_MIN = -30.0          # excludes silence (≪ -55 dBFS)
_TC_ASTATS_RMS_LEVEL_MAX = -10.0
_TC_ASTATS_CREST_FACTOR_MIN = 1.55        # excludes pure sine tone (≈1.41)
# Mean per-channel crest of biphase mark TC clusters at ~2.0–2.22 in practice;
# 2.25 keeps the program-audio rejection (≈3.0) while not fragmenting on
# rounding noise at the boundary.
_TC_ASTATS_CREST_FACTOR_MAX = 2.25
_TC_ASTATS_ZERO_CROSSINGS_RATE_MIN = 0.05  # excludes sub-kHz tones (≪0.05)
_TC_ASTATS_ZERO_CROSSINGS_RATE_MAX = 0.10  # excludes silence (≈0.45)
_TC_ASTATS_ENTROPY_MIN = 0.60             # excludes silence (≈0.28)
_TC_ASTATS_ENTROPY_MAX = 0.80

# Per-channel TC indicators in astats — stability gates (TC sits rock-stable;
# program audio swings every metric)
_TC_ASTATS_RMS_STDEV_MAX = 1.0
_TC_ASTATS_CREST_STDEV_MAX = 0.30
_TC_ASTATS_ZCR_STDEV_MAX = 0.02
_TC_ASTATS_ENTROPY_STDEV_MAX = 0.05

# Per-frame gap refinement (astats). The 30s windowed stability gate above is
# easily poisoned: a brief silence or carrier dropout inside a window spikes the
# window's stdev and rejects the whole 30s, so a ~3s real gap gets reported as a
# ~100s hole and tens of seconds of valid TC on either side are discarded. After
# the windowed pass has identified TC territory, we re-scan it per frame to
# recover precise boundaries: a frame counts as TC only if all four astats
# metrics sit in their LTC bands (reusing the band constants above), and a gap
# is a sustained run of non-TC frames.
_TC_GAP_REFINE_SMOOTH_FRAMES = 3   # majority vote over +/- this many frames
_TC_GAP_MERGE_SEC = 1.0            # merge non-TC runs split by a shorter TC flicker
_TC_GAP_MIN_SEC = 2.0              # minimum sustained non-TC run to count as a real gap
_TC_GAP_BRIDGE_SEC = 120.0         # bridge windowed regions up to this gap, then re-split
_TC_GAP_REFINE_MARGIN_SEC = 15.0   # extend each bridged span by this much before re-scanning

# ---------------------------------------------------------------------------
# Audio dropout detection thresholds
# ---------------------------------------------------------------------------

DROPOUT_ROLLING_WINDOW_SIZE = 7       # ~11s of audio at ~1.6s/frame
DROPOUT_RMS_DROP_THRESHOLD_DB = 40.0  # dB drop below rolling median to trigger
DROPOUT_SILENCE_FLOOR_DB = -55.0      # ignore frames where median is below this (natural silence)
DROPOUT_DIFF_DROP_FACTOR = 0.1        # Max/RMS_difference must drop below 10% of rolling median
DROPOUT_ZCR_SPIKE_FACTOR = 3.0        # Zero_crossings_rate must exceed 3x rolling median
DROPOUT_MERGE_GAP_SEC = 3.5           # merge candidates within this gap on same channel
DROPOUT_LONG_EVENT_SEC = 2.0          # events longer than this require high confidence
DROPOUT_LONG_EVENT_MIN_CORR = 2       # minimum corroborating metrics for long events
DROPOUT_BARS_MARGIN_SEC = 1.0         # suppress candidates this long past a bars region (tone-off cliff)
DROPOUT_ZCR_XCHAN_TOLERANCE_SEC = 0.5  # ZCR-only events need another channel firing within this window

# ---------------------------------------------------------------------------
# Identical channel (dual mono) detection thresholds
# ---------------------------------------------------------------------------

# Screening stage — runs off the astats/aphasemeter values already parsed from
# the report, so it costs nothing. Two channels "match" on a frame when their
# RMS levels agree within the tolerance; duplicated channels agree exactly, so
# the tolerance only has to absorb astats' one-decimal rounding.
IDENTICAL_RMS_TOLERANCE_DB = 0.1
# aphasemeter reports the inter-channel correlation of a stereo pair: +1 is a
# perfectly in-phase (duplicated) pair, -1 a polarity-inverted duplicate. Only
# available for 2-channel audio; other channel counts screen on RMS alone.
IDENTICAL_PHASE_THRESHOLD = 0.999
# Frames whose loudest channel sits at or below this are silence — both
# channels match trivially there, so they don't count toward the comparison.
IDENTICAL_SILENCE_FLOOR_DB = SILENCE_THRESHOLD_DB
# Too few frames with audio to say anything either way.
IDENTICAL_MIN_ACTIVE_FRAMES = 10
# Fraction of active frames that must match for a whole-file verdict, and the
# floor below which a file is simply reported as having distinct channels.
IDENTICAL_FULL_MATCH_FRACTION = 0.99
IDENTICAL_PARTIAL_MATCH_FRACTION = 0.10
# Matching runs shorter than this aren't reported as regions (a few frames of
# coincidentally equal level prove nothing).
IDENTICAL_MIN_SPAN_SEC = 5.0
# At most this many channel pairs get the ffmpeg confirmation pass, so a
# many-channel file can't turn into a pile of decodes. Stereo has one pair.
IDENTICAL_MAX_CONFIRM_PAIRS = 3

# Confirmation stage — sample-level comparison of the two channels.
# A difference peak at or below this is digital silence for any practical bit
# depth (true bit-identical channels measure -inf).
IDENTICAL_BIT_IDENTICAL_PEAK_DB = -100.0
# Otherwise the channels count as the same content when their difference peaks
# this far below the louder channel's own peak. 30 dB is a ~3% residual: it
# covers a duplicate carrying a hair of level offset or different dither (a 1%
# gain difference alone puts the residual at exactly -40 dB, so a tighter
# margin would contradict the screen's 0.1 dB RMS tolerance), while genuine
# stereo program material leaves a residual within ~10 dB of program peak.
IDENTICAL_EFFECTIVE_MARGIN_DB = 30.0
# Confirmation reads this many sample frames per chunk from the ffmpeg pipe.
_IDENTICAL_CHUNK_SAMPLES = 1 << 18


@dataclass
class _TCDetection:
    """A detected region of audible timecode."""
    start_time: float = 0.0
    end_time: float = 0.0
    criterion: str = ""
    channel: str = ""
    confidence: str = ""
    details: str = ""


@dataclass
class _DropoutCandidate:
    """A single-frame dropout candidate before merging."""
    time: float = 0.0
    channel: int = 0
    rms_level: float = 0.0
    median_rms: float = 0.0
    corroborating: list = field(default_factory=list)


@dataclass
class _DropoutEvent:
    """A merged dropout event (one or more consecutive candidates)."""
    start_time: float = 0.0
    end_time: float = 0.0
    channel: int = 0
    worst_rms_level: float = 0.0
    median_rms_level: float = 0.0
    confidence: str = ""
    corroborating: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Clamped levels detection — broadcast-range limits by bit depth
# ---------------------------------------------------------------------------

# An ADC that clamps at broadcast (legal) range will truncate excursions past
# these limits. Values are native to the bit depth reported by QCTools.
CLAMP_LIMITS_10BIT = {
    'Y':  {'floor': 64,  'ceiling': 940},
    'UV': {'floor': 64,  'ceiling': 960},
}
CLAMP_LIMITS_8BIT = {
    'Y':  {'floor': 16,  'ceiling': 235},
    'UV': {'floor': 16,  'ceiling': 240},
}

# Tolerance window (in code values) inside the legal range from each broadcast
# limit. Frames whose extreme lands in this window count as evidence of
# clamping. 8-bit requires an exact hit; 10-bit allows ±2 codes (≈ the same
# IRE precision as 8-bit) to absorb dithering and ADC calibration drift near
# the wall.
CLAMP_TOLERANCE_8BIT = 0
CLAMP_TOLERANCE_10BIT = 2

# A clamp is flagged when at least this many frames sit at or near the limit
# AND at least this percentage of total frames do, with zero excursions past.
CLAMP_MIN_HIT_FRAMES = 10
CLAMP_MIN_HIT_PCT = 1.0


def analyzeClampedLevels(startObj, pkt, report_directory, bit_depth_10, signals=None, total_duration=None):
    """
    Detect clamped video levels — an ADC artifact where signal excursions are
    truncated at the broadcast (legal) range limits. Flags a clamp when, for a
    given channel/direction, frames pile up at (or within a bit-depth-scaled
    tolerance of) the broadcast limit and zero frames go past it.

    Parameters:
        startObj (str): Path to the QCTools report file (.qctools.xml.gz).
        pkt (str): Timestamp attribute key (pkt_dts_time or pkt_pts_time).
        report_directory (str): Path to {video_id}_report_csvs directory for CSV output.
        bit_depth_10 (bool): True for 10-bit-scale values (0-1023), False for 8-bit (0-255).
        signals: Optional signals object for progress updates.
        total_duration (float or None): Total video duration in seconds for progress reporting.

    Returns:
        dict or None: Results dict (also written to CSV), or None on failure / no frames.
    """
    etree = load_etree()
    if etree is None:
        return None

    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for clamped-levels analysis")
        return None

    limits = CLAMP_LIMITS_10BIT if bit_depth_10 else CLAMP_LIMITS_8BIT
    tolerance = CLAMP_TOLERANCE_10BIT if bit_depth_10 else CLAMP_TOLERANCE_8BIT

    stats = {
        ch: {
            'floor': limits['Y' if ch == 'Y' else 'UV']['floor'],
            'ceiling': limits['Y' if ch == 'Y' else 'UV']['ceiling'],
            'min': None, 'max': None,
            'hit_floor': 0, 'hit_ceiling': 0,
            'below_floor': 0, 'above_ceiling': 0,
        }
        for ch in ('Y', 'U', 'V')
    }

    # Per-frame trace data, kept for graphing any channel/direction that ends
    # up flagged as clamped. Six parallel value arrays keyed by metric.
    trace_times = []
    trace_values = {m: [] for m in ('YMIN', 'YMAX', 'UMIN', 'UMAX', 'VMIN', 'VMAX')}

    total_frames = 0
    progress_interval = 500
    last_pct = 36

    try:
        for event, elem in parser_iter:
            if elem.attrib.get('media_type') == 'video':
                total_frames += 1

                try:
                    frame_time = float(elem.attrib.get(pkt, "0"))
                except (TypeError, ValueError):
                    frame_time = float(total_frames)
                trace_times.append(frame_time)

                if (signals and hasattr(signals, 'qctparse_progress') and
                        total_duration and total_frames % progress_interval == 0):
                    pct = 36 + int((frame_time / total_duration) * 28)
                    pct = min(64, max(36, pct))
                    if pct > last_pct:
                        signals.qctparse_progress.emit(pct)
                        last_pct = pct

                frame_metrics = {m: None for m in trace_values}

                for t in list(elem):
                    key = t.attrib.get('key', '')
                    if not key.startswith('lavfi.signalstats.'):
                        continue
                    metric = key.rsplit('.', 1)[-1]
                    if metric not in ('YMIN', 'UMIN', 'VMIN', 'YMAX', 'UMAX', 'VMAX'):
                        continue
                    try:
                        val = float(t.attrib['value'])
                    except (ValueError, KeyError):
                        continue

                    frame_metrics[metric] = val
                    s = stats[metric[0]]
                    if metric.endswith('MIN'):
                        if s['min'] is None or val < s['min']:
                            s['min'] = val
                        if val < s['floor']:
                            s['below_floor'] += 1
                        elif val <= s['floor'] + tolerance:
                            s['hit_floor'] += 1
                    else:
                        if s['max'] is None or val > s['max']:
                            s['max'] = val
                        if val > s['ceiling']:
                            s['above_ceiling'] += 1
                        elif val >= s['ceiling'] - tolerance:
                            s['hit_ceiling'] += 1

                for m, v in frame_metrics.items():
                    trace_values[m].append(v)

            elem.clear()
    except Exception as e:
        logger.error(f"Error during clamped-levels analysis: {e}")
        return None

    if total_frames == 0:
        logger.warning("No video frames found in QCTools report for clamped-levels analysis\n")
        return None

    findings = []
    any_clamp = False
    for ch in ('Y', 'U', 'V'):
        s = stats[ch]
        for direction in ('floor', 'ceiling'):
            if direction == 'floor':
                limit, hits, beyond, extreme = s['floor'], s['hit_floor'], s['below_floor'], s['min']
            else:
                limit, hits, beyond, extreme = s['ceiling'], s['hit_ceiling'], s['above_ceiling'], s['max']

            hit_pct = (hits / total_frames) * 100 if total_frames else 0.0
            sufficient_hits = hits >= CLAMP_MIN_HIT_FRAMES and hit_pct >= CLAMP_MIN_HIT_PCT

            if beyond > 0:
                verdict = 'Not Clamped'
            elif sufficient_hits:
                verdict = 'Clamped'
                any_clamp = True
            else:
                verdict = 'Inconclusive'

            findings.append({
                'channel': ch,
                'direction': direction,
                'limit': limit,
                'global_extreme': extreme,
                'hits': hits,
                'hit_pct': hit_pct,
                'beyond': beyond,
                'verdict': verdict,
            })

    results = {
        'total_video_frames': total_frames,
        'bit_depth_10': bit_depth_10,
        'tolerance': tolerance,
        'findings': findings,
        'any_clamp_detected': any_clamp,
    }

    _write_clamped_levels_results(report_directory, results)
    if any_clamp:
        _write_clamped_levels_traces(
            report_directory, findings, trace_times, trace_values
        )
    return results


CLAMP_TRACE_TARGET_POINTS = 5000


def _downsample_trace(times, values, target_points, mode):
    """
    Bucket a per-frame trace down to ~target_points by selecting the per-bucket
    extreme. mode='max' keeps the bucket maximum (for ceiling traces),
    mode='min' keeps the bucket minimum (for floor traces). Preserves brief
    wall-hit events that mean-based downsampling would smooth away.
    """
    n = len(values)
    if n == 0:
        return [], []
    pairs = [(t, v) for t, v in zip(times, values) if v is not None]
    if not pairs:
        return [], []
    if len(pairs) <= target_points:
        return [p[0] for p in pairs], [p[1] for p in pairs]
    bucket_size = max(1, math.ceil(len(pairs) / target_points))
    out_times, out_values = [], []
    select = max if mode == 'max' else min
    for i in range(0, len(pairs), bucket_size):
        chunk = pairs[i:i + bucket_size]
        t_pick, v_pick = select(chunk, key=lambda p: p[1])
        out_times.append(t_pick)
        out_values.append(v_pick)
    return out_times, out_values


def _write_clamped_levels_traces(report_directory, findings, trace_times, trace_values):
    """
    Write downsampled per-frame traces for each channel/direction flagged as
    Clamped. One CSV with all traces in long format so the report can plot
    each one as its own graph.
    """
    csv_path = os.path.join(report_directory, "qct-parse_clamped_traces.csv")
    rows_to_write = []
    for r in findings:
        if r['verdict'] != 'Clamped':
            continue
        metric = f"{r['channel']}{'MIN' if r['direction'] == 'floor' else 'MAX'}"
        mode = 'min' if r['direction'] == 'floor' else 'max'
        times, values = _downsample_trace(
            trace_times, trace_values[metric], CLAMP_TRACE_TARGET_POINTS, mode
        )
        for t, v in zip(times, values):
            rows_to_write.append([
                r['channel'], r['direction'], r['limit'], f"{t:.4f}", f"{v:g}"
            ])

    if not rows_to_write:
        return

    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["channel", "direction", "limit", "time_seconds", "value"])
        writer.writerows(rows_to_write)
    logger.debug(f"Clamped-levels trace data written to {os.path.basename(csv_path)}\n")


def _write_clamped_levels_results(report_directory, results):
    """Write clamped-levels detection results to CSV and log a summary."""
    csv_path = os.path.join(report_directory, "qct-parse_clamped_levels.csv")
    total_frames = results['total_video_frames']
    bit_depth = 10 if results['bit_depth_10'] else 8
    tolerance = results.get('tolerance', 0)

    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Clamped Levels Detection Results"])
        writer.writerow(["Bit Depth", bit_depth])
        writer.writerow(["Total Video Frames", total_frames])
        writer.writerow(["Tolerance Window (codes)", tolerance])
        writer.writerow(["Min Hit Frames (absolute)", CLAMP_MIN_HIT_FRAMES])
        writer.writerow(["Min Hit Frames (%)", f"{CLAMP_MIN_HIT_PCT:.2f}"])
        writer.writerow(["Any Clamp Detected", "Yes" if results['any_clamp_detected'] else "No"])
        writer.writerow([])

        writer.writerow([
            "Channel", "Direction", "Limit", "Global Extreme",
            "Frames at/near Limit", "Hit %", "Frames Beyond Limit", "Verdict"
        ])
        for r in results['findings']:
            extreme_str = f"{r['global_extreme']:g}" if r['global_extreme'] is not None else "N/A"
            writer.writerow([
                r['channel'], r['direction'], r['limit'], extreme_str,
                r['hits'], f"{r['hit_pct']:.2f}", r['beyond'], r['verdict']
            ])

    if results['any_clamp_detected']:
        clamped_summary = ", ".join(
            f"{r['channel']}-{r['direction']}"
            for r in results['findings'] if r['verdict'] == 'Clamped'
        )
        logger.warning(f"Clamped levels detected: {clamped_summary}. See {os.path.basename(csv_path)}\n")
    else:
        logger.debug(f"No clamped levels detected (bit depth {bit_depth}). Results in {os.path.basename(csv_path)}\n")


# Chroma phase error detection thresholds. Empirically derived from a known-
# affected analog source (helical-scan tracking errors producing cyan/magenta
# colour shifts with horizontal image displacement) and validated against a
# clean source. Two-rule detector:
#   PRIMARY  - chroma "envelope wide": within a single frame, both U and V
#              span nearly the full chroma range (UMIN low AND UMAX high AND
#              VMIN low AND VMAX high). This is the strongest single-frame
#              signature of a chroma phase event.
#   SECONDARY - SATMAX over a high threshold. Catches partial events where
#              only a portion of the frame is in chroma phase error.
# Thresholds below are 10-bit (0-1023). For 8-bit (0-255) they are scaled /4.
CHROMA_ENVELOPE_LOW_10BIT = 100
CHROMA_ENVELOPE_HIGH_10BIT = 900
CHROMA_SATMAX_HIGH_10BIT = 600
# Consecutive flagged frames within this gap (in frames) are merged into a
# single event. ~10 frames = ~333ms at 29.97fps. A single chroma phase event
# often produces non-contiguous flagged frames clustered within ~half a second.
CHROMA_EVENT_GAP_FRAMES = 10
# Minimum event length (in frames). Events of length 1 are kept only when the
# single frame fires BOTH the envelope rule AND the SATMAX rule — that combo
# is the high-confidence chroma-flip signature. Single-frame events firing
# only one of the two rules are filtered as transients (bright frames with
# rogue outlier pixels, scene cuts that briefly spike SATMAX, etc.).
CHROMA_MIN_EVENT_FRAMES = 2
CHROMA_SINGLE_FRAME_REQUIRES_BOTH_RULES = True
# Cap the number of thumbnails generated per video. When more events are
# detected, the top-N by frame_count are selected.
CHROMA_MAX_THUMBS = 10
# Subdirectory inside report_directory where chroma-phase thumbnails are
# saved. Kept separate from ThumbExports/ so find_qct_thumbs() does not pick
# them up and surface them under unrelated report sections.
CHROMA_THUMBS_DIRNAME = "ChromaPhaseThumbs"


def analyzeChromaPhaseErrors(startObj, pkt, report_directory, bit_depth_10,
                             bars_end_time=None, video_path=None,
                             signals=None, total_duration=None):
    """
    Detect chroma phase errors - typically caused by helical-scan tracking
    failures on tape source, producing sudden shifts of the chroma signal so
    that all colour collapses toward cyan or magenta, often with horizontal
    image displacement. Walks the QCTools report once, flags individual
    frames against two rules, then merges consecutive flagged frames into
    events. When ``video_path`` is provided, also exports thumbnail PNGs at
    the peak frame of the top-N events (by frame count) for the HTML report.

    Parameters:
        startObj (str): Path to the QCTools report file (.qctools.xml.gz).
        pkt (str): Timestamp attribute key (pkt_dts_time or pkt_pts_time).
        report_directory (str): Path to {video_id}_report_csvs directory.
        bit_depth_10 (bool): True for 10-bit-scale values (0-1023), False for 8-bit.
        bars_end_time (float or None): Skip frames with timestamp < bars_end_time
            to avoid flagging SMPTE colour bars at the head of the tape.
        video_path (str or None): Path to the source video file; when present,
            thumbnails are generated for the top-N events by frame count.
        signals: Optional signals object for emitting progress updates.
        total_duration (float or None): Total video duration in seconds for progress.

    Returns:
        dict or None: Results dict (also written to CSV), or None on failure.
    """
    etree = load_etree()
    if etree is None:
        return None

    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for chroma-phase analysis")
        return None

    if bit_depth_10:
        env_low = CHROMA_ENVELOPE_LOW_10BIT
        env_high = CHROMA_ENVELOPE_HIGH_10BIT
        satmax_high = CHROMA_SATMAX_HIGH_10BIT
    else:
        env_low = CHROMA_ENVELOPE_LOW_10BIT / 4
        env_high = CHROMA_ENVELOPE_HIGH_10BIT / 4
        satmax_high = CHROMA_SATMAX_HIGH_10BIT / 4

    METRIC_KEYS = ('UMIN', 'UMAX', 'VMIN', 'VMAX', 'SATMAX', 'HUEMED', 'SATAVG')

    flagged = []  # list of dicts: {'frame_idx', 'time', 'rule', 'envelope_wide',
                  # 'satmax_hit', 'satmax', 'huemed', 'satavg'}
    total_frames = 0
    frames_skipped_bars = 0
    progress_interval = 500
    last_pct = 64

    try:
        for event, elem in parser_iter:
            if elem.attrib.get('media_type') != 'video':
                elem.clear()
                continue
            total_frames += 1

            try:
                frame_time = float(elem.attrib.get(pkt, "0"))
            except (TypeError, ValueError):
                frame_time = float(total_frames)

            if (signals and hasattr(signals, 'qctparse_progress') and
                    total_duration and total_frames % progress_interval == 0):
                pct = 64 + int((frame_time / total_duration) * 28)
                pct = min(92, max(64, pct))
                if pct > last_pct:
                    signals.qctparse_progress.emit(pct)
                    last_pct = pct

            if bars_end_time is not None and frame_time < bars_end_time:
                frames_skipped_bars += 1
                elem.clear()
                continue

            metrics = {}
            for t in list(elem):
                key = t.attrib.get('key', '')
                if not key.startswith('lavfi.signalstats.'):
                    continue
                metric = key.rsplit('.', 1)[-1]
                if metric not in METRIC_KEYS:
                    continue
                try:
                    metrics[metric] = float(t.attrib['value'])
                except (ValueError, KeyError):
                    continue
            elem.clear()

            if not metrics:
                continue

            umin = metrics.get('UMIN')
            umax = metrics.get('UMAX')
            vmin = metrics.get('VMIN')
            vmax = metrics.get('VMAX')
            satmax = metrics.get('SATMAX', 0)

            envelope_wide = (
                umin is not None and umax is not None
                and vmin is not None and vmax is not None
                and umin < env_low and umax > env_high
                and vmin < env_low and vmax > env_high
            )
            satmax_hit = satmax > satmax_high

            if envelope_wide or satmax_hit:
                flagged.append({
                    'frame_idx': total_frames - 1,
                    'time': frame_time,
                    'rule': 'envelope' if envelope_wide else 'satmax',
                    'envelope_wide': envelope_wide,
                    'satmax_hit': satmax_hit,
                    'satmax': satmax,
                    'huemed': metrics.get('HUEMED'),
                    'satavg': metrics.get('SATAVG'),
                })
    except Exception as e:
        logger.error(f"Error during chroma-phase analysis: {e}")
        return None

    if total_frames == 0:
        logger.warning("No video frames found in QCTools report for chroma-phase analysis\n")
        return None

    # Group consecutive flagged frames into events (gap <= CHROMA_EVENT_GAP_FRAMES)
    events = []
    cur = []
    for f in flagged:
        if not cur or (f['frame_idx'] - cur[-1]['frame_idx']) <= CHROMA_EVENT_GAP_FRAMES:
            cur.append(f)
        else:
            events.append(cur)
            cur = [f]
    if cur:
        events.append(cur)

    # Filter events. An event is kept if it has >= CHROMA_MIN_EVENT_FRAMES
    # frames, OR if it is a single high-confidence frame (envelope AND satmax
    # both fired) when CHROMA_SINGLE_FRAME_REQUIRES_BOTH_RULES is enabled.
    def _keep(e):
        if len(e) >= CHROMA_MIN_EVENT_FRAMES:
            return True
        if CHROMA_SINGLE_FRAME_REQUIRES_BOTH_RULES and len(e) == 1:
            f = e[0]
            return bool(f.get('envelope_wide') and f.get('satmax_hit'))
        return False
    events = [e for e in events if _keep(e)]

    # Build per-event summary records
    event_records = []
    for idx, e in enumerate(events):
        peak = max(e, key=lambda f: f['satmax'])
        rules = {f['rule'] for f in e}
        # 'envelope' is stricter than 'satmax'; report whichever rules fired
        rule_str = '+'.join(sorted(rules))
        event_records.append({
            'event_idx': idx + 1,
            'start_time': e[0]['time'],
            'end_time': e[-1]['time'],
            'duration_seconds': e[-1]['time'] - e[0]['time'],
            'frame_count': len(e),
            'rule': rule_str,
            'peak_satmax': peak['satmax'],
            'peak_time': peak['time'],
            'hue_at_peak': peak['huemed'],
        })

    # Generate thumbnails for the top-N events (by frame count). Saves to
    # report_directory/ChromaPhaseThumbs/event_NN.png, keyed by the event's
    # 1-based index so the HTML renderer can match thumbs to event rows.
    thumbs_written = 0
    if video_path and event_records:
        top_events = sorted(event_records, key=lambda e: e['frame_count'], reverse=True)[:CHROMA_MAX_THUMBS]
        thumb_dir = os.path.join(report_directory, CHROMA_THUMBS_DIRNAME)
        try:
            os.makedirs(thumb_dir, exist_ok=True)
            for e in top_events:
                out_path = os.path.join(thumb_dir, f"event_{e['event_idx']:02d}.png")
                if _export_chroma_thumb(video_path, e['peak_time'], out_path):
                    thumbs_written += 1
        except Exception as ex:
            logger.warning(f"Chroma-phase thumbnail generation failed: {ex}")

    results = {
        'total_video_frames': total_frames,
        'bit_depth_10': bit_depth_10,
        'bars_skipped_frames': frames_skipped_bars,
        'flagged_frames': len(flagged),
        'event_count': len(event_records),
        'events': event_records,
        'thumbnails_written': thumbs_written,
        'thresholds': {
            'envelope_low': env_low,
            'envelope_high': env_high,
            'satmax_high': satmax_high,
        },
    }

    _write_chroma_phase_results(report_directory, results)
    return results


def _export_chroma_thumb(video_path, time_seconds, out_path):
    """
    Render a single PNG thumbnail from ``video_path`` at ``time_seconds`` to
    ``out_path``. No signalstats overlay - we want the unmodified frame so
    the chroma cast and image displacement are visible. Returns True on
    success.
    """
    if not video_path or not os.path.isfile(video_path):
        logger.warning(f"Chroma-phase thumbnail skipped: video file not found at {video_path}")
        return False
    try:
        ts_str = dts2ts(str(time_seconds))
    except Exception:
        ts_str = f"{time_seconds:.3f}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", ts_str,
        "-i", video_path,
        "-vframes", "1",
        "-s", "720x486",
        out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning(f"ffmpeg failed for chroma-phase thumbnail at {ts_str} (exit code {result.returncode})")
            report_ffmpeg_stderr(result.stderr, "chroma-phase thumbnail", failure=True)
            return False
        return os.path.isfile(out_path)
    except subprocess.TimeoutExpired:
        logger.warning(f"ffmpeg timed out generating chroma-phase thumbnail at {ts_str}")
        return False
    except Exception as e:
        logger.warning(f"Error generating chroma-phase thumbnail at {ts_str}: {e}")
        return False


def _write_chroma_phase_results(report_directory, results):
    """Write chroma-phase detection results to two CSVs (summary + per-event)."""
    summary_path = os.path.join(report_directory, "qct-parse_chroma_phase_summary.csv")
    events_path = os.path.join(report_directory, "qct-parse_chroma_phase_events.csv")

    total = results['total_video_frames']
    flagged = results['flagged_frames']
    bit_depth = 10 if results['bit_depth_10'] else 8
    th = results['thresholds']

    with open(summary_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Chroma Phase Error Detection Summary"])
        writer.writerow(["Bit Depth", bit_depth])
        writer.writerow(["Total Video Frames", total])
        writer.writerow(["Frames Skipped (color bars region)", results['bars_skipped_frames']])
        writer.writerow(["Flagged Frames", flagged])
        writer.writerow(["Flagged %", f"{(flagged/total*100) if total else 0:.3f}"])
        writer.writerow(["Detected Events", results['event_count']])
        writer.writerow([])
        writer.writerow(["Thresholds"])
        writer.writerow(["Envelope Low (U/V min)", f"{th['envelope_low']:g}"])
        writer.writerow(["Envelope High (U/V max)", f"{th['envelope_high']:g}"])
        writer.writerow(["SATMAX High", f"{th['satmax_high']:g}"])
        writer.writerow(["Event Merge Gap (frames)", CHROMA_EVENT_GAP_FRAMES])

    with open(events_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            "Event", "Start (HH:MM:SS.mmm)", "End (HH:MM:SS.mmm)",
            "Duration (s)", "Frames", "Rule",
            "Peak SATMAX", "Peak Time (HH:MM:SS.mmm)", "Hue at Peak (deg)",
        ])
        for r in results['events']:
            writer.writerow([
                r['event_idx'],
                dts2ts(str(r['start_time'])),
                dts2ts(str(r['end_time'])),
                f"{r['duration_seconds']:.3f}",
                r['frame_count'],
                r['rule'],
                f"{r['peak_satmax']:g}",
                dts2ts(str(r['peak_time'])),
                f"{r['hue_at_peak']:g}" if r['hue_at_peak'] is not None else "",
            ])

    if results['event_count']:
        logger.warning(
            f"Chroma phase errors detected: {results['event_count']} events "
            f"({flagged} frames). See {os.path.basename(events_path)}\n"
        )
    else:
        logger.debug(
            f"No chroma phase errors detected (bit depth {bit_depth}). "
            f"Results in {os.path.basename(summary_path)}\n"
        )


def analyzeAudio(startObj, pkt, report_directory, detect_clipping=False, detect_imbalance=False, detect_timecode=False, detect_dropout=False, detect_identical=False, signals=None, total_duration=None, fps=None, tc_start_frames=0, tc_drop_frame=False, bars_regions=None, video_path=None, audio_stream_indexes=None, check_cancelled=None):
    """
    Analyzes audio frames in a QCTools report in a single pass. Optionally detects
    audio clipping (Peak_level >= threshold), channel imbalance (comparing
    per-channel RMS_level values), audible timecode (LTC artifacts), audio
    dropout (sudden RMS drops indicative of tape dropout), and/or identical
    channels (the same audio duplicated across two channels — effectively mono).

    Parameters:
        startObj (str): Path to the QCTools report file (.qctools.xml.gz)
        pkt (str): The attribute key used to extract timestamps (pkt_dts_time or pkt_pts_time).
        report_directory (str): Path to {video_id}_report_csvs directory for CSV output.
        detect_clipping (bool): Whether to run audio clipping detection.
        detect_imbalance (bool): Whether to run channel imbalance detection.
        detect_timecode (bool): Whether to run audible timecode detection.
        detect_dropout (bool): Whether to run audio dropout detection.
        detect_identical (bool): Whether to run identical-channel (dual mono) detection.
        signals: Optional signals object for emitting progress updates.
        total_duration (float or None): Total video duration in seconds for progress reporting.
        fps (float or None): Video frame rate, used to format audible-timecode region
            positions as timecode matching an NLE. Defaults to NTSC 29.97 when None.
        tc_start_frames (int): Stream start-timecode offset in frames (from the file's
            TIMECODE tag), added to every reported position.
        tc_drop_frame (bool): Whether the stream timecode is drop-frame, so positions
            are formatted as DF (HH:MM:SS;FF) rather than NDF.
        video_path (str or None): Path to the video file. Identical-channel detection
            uses it for its sample-level confirmation pass; without it the verdict
            rests on the report metrics alone.
        audio_stream_indexes (list or None): Audio-relative stream indexes (the N of
            ``0:a:N``) behind the analyzed channels, in the order the audio stats
            sidecar merged them, for multi-stream (discrete mono) inputs. None means
            a single stream carries every channel.
        check_cancelled (callable or None): Returns True when processing has been
            cancelled; polled during the identical-channel confirmation decode.

    Returns:
        tuple: (clipping_results, imbalance_results, timecode_results, dropout_results,
            identical_results). Each is a dict with analysis results, or None if that
            analysis was not requested or no audio frames were found.
    """
    etree = load_etree()
    if etree is None:
        return None, None, None, None, None

    parser_iter = safe_gzip_iterparse(startObj, etree)
    if parser_iter is None:
        logger.error(f"Failed to parse {startObj} for audio analysis")
        return None, None, None, None, None

    total_audio_frames = 0

    # Progress emission setup: maps into the 64→99% range of qctparse_progress
    audio_progress_interval = 500
    last_audio_progress_pct = 64

    # Clipping state
    clipping_events = []
    clipped_frames = 0
    max_peak_level = None
    max_flat_factor = None

    # Channel imbalance state — dict keyed by channel number (int), values are lists of RMS levels
    channel_rms_values = {}

    # Timecode detection state — per-frame data collected during parse
    tc_frames = []  # list of dicts: {time, tags}
    tc_metric_type = 'unknown'

    # Identical-channel state — per channel pair, keyed by (ch_a, ch_b)
    identical_pairs = {}

    # Dropout detection state — per-channel rolling windows and candidates
    dropout_candidates = []  # list of _DropoutCandidate
    # Per-channel rolling windows: {ch_num: deque of values}
    dropout_rms_windows = {}
    dropout_max_diff_windows = {}
    dropout_rms_diff_windows = {}
    dropout_zcr_windows = {}

    try:
        for event, elem in parser_iter:
            if elem.attrib.get('media_type') == "audio":
                total_audio_frames += 1
                frame_pkt_dts_time = elem.attrib.get(pkt, "0")

                # Emit incremental progress during audio analysis (64→99% range)
                if (signals and hasattr(signals, 'qctparse_progress') and
                    total_duration and total_audio_frames % audio_progress_interval == 0):
                    pct = 64 + int((float(frame_pkt_dts_time) / total_duration) * 35)
                    pct = min(99, max(64, pct))
                    if pct > last_audio_progress_pct:
                        signals.qctparse_progress.emit(pct)
                        last_audio_progress_pct = pct

                # Parse audio frame attributes in one loop
                peak_level = None
                flat_factor = None
                frame_channel_rms = {}
                frame_phase = None
                tc_frame_tags = {} if detect_timecode else None
                # Dropout per-frame state: {ch_num: {metric: value}}
                dropout_frame_data = {} if detect_dropout else None

                for t in list(elem):
                    key = t.attrib.get('key', '')
                    try:
                        val = float(t.attrib['value'])
                    except (ValueError, KeyError):
                        continue

                    if detect_clipping:
                        if key.endswith('.Peak_level') and 'Overall' in key:
                            peak_level = val
                        elif key.endswith('.Flat_factor') and 'Overall' in key:
                            flat_factor = val

                    if detect_imbalance or detect_identical:
                        # Match lavfi.astats.N.RMS_level for any channel number N
                        if key.endswith('.RMS_level') and 'Overall' not in key:
                            match = re.search(r'\.(\d+)\.RMS_level$', key)
                            if match:
                                ch_num = int(match.group(1))
                                frame_channel_rms[ch_num] = val

                    if detect_identical and key == 'lavfi.aphasemeter.phase':
                        frame_phase = val

                    if detect_timecode:
                        # Collect r128 and astats tags for timecode detection.
                        # Track whichever metric families appear; if both are
                        # present, the detector runs both and unions results.
                        if key.startswith('lavfi.r128'):
                            tc_frame_tags[key] = val
                            if tc_metric_type == 'unknown':
                                tc_metric_type = 'r128'
                            elif tc_metric_type == 'astats':
                                tc_metric_type = 'both'
                        elif key.startswith('lavfi.astats'):
                            tc_frame_tags[key] = val
                            if tc_metric_type == 'unknown':
                                tc_metric_type = 'astats'
                            elif tc_metric_type == 'r128':
                                tc_metric_type = 'both'

                    if detect_dropout and 'Overall' not in key:
                        # Extract per-channel dropout metrics
                        for metric in ('RMS_level', 'Max_difference', 'RMS_difference', 'Zero_crossings_rate'):
                            if key.endswith(f'.{metric}'):
                                match = re.search(r'\.(\d+)\.' + metric + '$', key)
                                if match:
                                    ch_num = int(match.group(1))
                                    if ch_num not in dropout_frame_data:
                                        dropout_frame_data[ch_num] = {}
                                    dropout_frame_data[ch_num][metric] = val
                                break

                # Clipping analysis
                if detect_clipping and peak_level is not None:
                    if max_peak_level is None or peak_level > max_peak_level:
                        max_peak_level = peak_level
                    if flat_factor is not None and (max_flat_factor is None or flat_factor > max_flat_factor):
                        max_flat_factor = flat_factor

                    if peak_level >= AUDIO_CLIPPING_THRESHOLD_DB:
                        clipped_frames += 1
                        timeStampString = dts2ts(frame_pkt_dts_time)
                        clipping_events.append({
                            'timestamp': timeStampString,
                            'timestamp_seconds': float(frame_pkt_dts_time),
                            'peak_level': peak_level,
                            'flat_factor': flat_factor
                        })

                # Channel imbalance collection
                if detect_imbalance:
                    for ch_num, rms_val in frame_channel_rms.items():
                        if ch_num not in channel_rms_values:
                            channel_rms_values[ch_num] = []
                        channel_rms_values[ch_num].append(rms_val)

                # Identical-channel screening — compare every channel pair on
                # this frame against the RMS/phase match criteria
                if detect_identical and len(frame_channel_rms) >= 2:
                    try:
                        frame_time = float(frame_pkt_dts_time)
                    except ValueError:
                        frame_time = 0.0
                    _identical_update_pairs(
                        identical_pairs, frame_time, frame_channel_rms, frame_phase
                    )

                # Timecode frame collection
                if detect_timecode and tc_frame_tags:
                    try:
                        frame_time = float(frame_pkt_dts_time)
                    except ValueError:
                        frame_time = 0.0
                    tc_frames.append({'time': frame_time, 'tags': tc_frame_tags})

                # Dropout detection — rolling window analysis per channel
                if detect_dropout and dropout_frame_data:
                    try:
                        frame_time = float(frame_pkt_dts_time)
                    except ValueError:
                        frame_time = 0.0

                    for ch_num, metrics in dropout_frame_data.items():
                        rms = metrics.get('RMS_level')
                        if rms is None:
                            continue

                        # Initialize rolling windows for this channel
                        if ch_num not in dropout_rms_windows:
                            dropout_rms_windows[ch_num] = collections.deque(maxlen=DROPOUT_ROLLING_WINDOW_SIZE)
                            dropout_max_diff_windows[ch_num] = collections.deque(maxlen=DROPOUT_ROLLING_WINDOW_SIZE)
                            dropout_rms_diff_windows[ch_num] = collections.deque(maxlen=DROPOUT_ROLLING_WINDOW_SIZE)
                            dropout_zcr_windows[ch_num] = collections.deque(maxlen=DROPOUT_ROLLING_WINDOW_SIZE)

                        rms_win = dropout_rms_windows[ch_num]
                        max_diff_win = dropout_max_diff_windows[ch_num]
                        rms_diff_win = dropout_rms_diff_windows[ch_num]
                        zcr_win = dropout_zcr_windows[ch_num]

                        # Only analyze once window is full
                        if len(rms_win) >= DROPOUT_ROLLING_WINDOW_SIZE:
                            median_rms = sorted(rms_win)[len(rms_win) // 2]

                            # Skip if content is near-silent (avoids false positives)
                            if median_rms >= DROPOUT_SILENCE_FLOOR_DB:
                                rms_drop = median_rms - rms
                                if rms_drop >= DROPOUT_RMS_DROP_THRESHOLD_DB:
                                    # Check corroborating metrics.
                                    # During dropout the signal drops to near-silence, so:
                                    #   - Max_difference DROPS (tiny sample-to-sample jumps)
                                    #   - RMS_difference DROPS (tiny sample-to-sample variation)
                                    #   - Zero_crossings_rate INCREASES (noise hovers around zero)
                                    corroborating = []
                                    max_diff = metrics.get('Max_difference')
                                    if max_diff is not None and len(max_diff_win) >= DROPOUT_ROLLING_WINDOW_SIZE:
                                        median_max_diff = sorted(max_diff_win)[len(max_diff_win) // 2]
                                        if median_max_diff > 0 and max_diff < median_max_diff * DROPOUT_DIFF_DROP_FACTOR:
                                            corroborating.append('Max_difference drop')

                                    rms_diff = metrics.get('RMS_difference')
                                    if rms_diff is not None and len(rms_diff_win) >= DROPOUT_ROLLING_WINDOW_SIZE:
                                        median_rms_diff = sorted(rms_diff_win)[len(rms_diff_win) // 2]
                                        if median_rms_diff > 0 and rms_diff < median_rms_diff * DROPOUT_DIFF_DROP_FACTOR:
                                            corroborating.append('RMS_difference drop')

                                    zcr = metrics.get('Zero_crossings_rate')
                                    if zcr is not None and len(zcr_win) >= DROPOUT_ROLLING_WINDOW_SIZE:
                                        median_zcr = sorted(zcr_win)[len(zcr_win) // 2]
                                        if median_zcr > 0 and zcr > median_zcr * DROPOUT_ZCR_SPIKE_FACTOR:
                                            corroborating.append('Zero_crossings_rate spike')

                                    dropout_candidates.append(_DropoutCandidate(
                                        time=frame_time,
                                        channel=ch_num,
                                        rms_level=rms,
                                        median_rms=median_rms,
                                        corroborating=corroborating,
                                    ))

                        # Update rolling windows (after analysis, so current frame doesn't influence its own detection)
                        rms_win.append(rms)
                        max_diff = metrics.get('Max_difference')
                        if max_diff is not None:
                            max_diff_win.append(max_diff)
                        rms_diff = metrics.get('RMS_difference')
                        if rms_diff is not None:
                            rms_diff_win.append(rms_diff)
                        zcr = metrics.get('Zero_crossings_rate')
                        if zcr is not None:
                            zcr_win.append(zcr)

            elem.clear()
    except Exception as e:
        logger.error(f"Error during audio analysis: {e}")

    if total_audio_frames == 0:
        logger.warning("No audio frames found in QCTools report for audio analysis\n")
        return None, None, None, None, None

    # Build clipping results
    clipping_results = None
    if detect_clipping:
        clipping_results = _write_clipping_results(
            report_directory, total_audio_frames, clipped_frames,
            max_peak_level, max_flat_factor, clipping_events
        )

    # Build channel imbalance results
    imbalance_results = None
    if detect_imbalance:
        imbalance_results = _write_imbalance_results(
            report_directory, total_audio_frames, channel_rms_values
        )

    # Run audible timecode detection
    timecode_results = None
    if detect_timecode:
        timecode_results = _detect_and_write_timecode_results(
            tc_frames, tc_metric_type, report_directory, fps=fps,
            tc_start_frames=tc_start_frames, tc_drop_frame=tc_drop_frame
        )

    # Run dropout detection
    dropout_results = None
    if detect_dropout:
        dropout_results = _detect_and_write_dropout_results(
            dropout_candidates, report_directory, total_audio_frames,
            fps=fps, tc_start_frames=tc_start_frames, tc_drop_frame=tc_drop_frame,
            bars_regions=bars_regions
        )

    # Run identical-channel (dual mono) detection
    identical_results = None
    if detect_identical:
        identical_results = _detect_and_write_identical_results(
            identical_pairs, report_directory, total_audio_frames,
            video_path=video_path, audio_stream_indexes=audio_stream_indexes,
            fps=fps, tc_start_frames=tc_start_frames, tc_drop_frame=tc_drop_frame,
            check_cancelled=check_cancelled
        )

    return clipping_results, imbalance_results, timecode_results, dropout_results, identical_results


def _write_clipping_results(report_directory, total_audio_frames, clipped_frames, max_peak_level, max_flat_factor, clipping_events):
    """Write audio clipping detection results to CSV and log summary."""
    clipping_detected = clipped_frames > 0
    pct = (clipped_frames / total_audio_frames) * 100 if total_audio_frames > 0 else 0

    results = {
        'clipping_detected': clipping_detected,
        'clipping_events': clipping_events,
        'total_audio_frames': total_audio_frames,
        'clipped_frames': clipped_frames,
        'max_peak_level': max_peak_level if max_peak_level is not None else 0.0,
        'max_flat_factor': max_flat_factor if max_flat_factor is not None else 0.0,
        'threshold_db': AUDIO_CLIPPING_THRESHOLD_DB
    }

    audio_clipping_csv = os.path.join(report_directory, "qct-parse_audio_clipping.csv")
    with open(audio_clipping_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Audio Clipping Detection Results"])
        writer.writerow(["Threshold (dBFS)", AUDIO_CLIPPING_THRESHOLD_DB])
        writer.writerow(["Total Audio Frames", total_audio_frames])
        writer.writerow(["Clipped Frames", clipped_frames])
        writer.writerow(["Clipped Frames (%)", f"{pct:.2f}"])
        writer.writerow(["Max Peak Level (dBFS)", f"{max_peak_level:.1f}" if max_peak_level is not None else "N/A"])
        writer.writerow(["Max Flat Factor", f"{max_flat_factor:.0f}" if max_flat_factor is not None else "N/A"])
        writer.writerow(["Clipping Detected", "Yes" if clipping_detected else "No"])
        writer.writerow([])

        if clipping_events:
            writer.writerow(["Timestamp", "Peak Level (dBFS)", "Flat Factor"])
            for event in clipping_events:
                ff = event.get('flat_factor')
                ff_str = f"{ff:.0f}" if ff is not None else "N/A"
                writer.writerow([event['timestamp'], f"{event['peak_level']:.1f}", ff_str])

    if clipping_detected:
        logger.warning(f"Audio clipping detected: {clipped_frames} frames ({pct:.2f}%) exceeded {AUDIO_CLIPPING_THRESHOLD_DB} dBFS threshold. Max peak: {max_peak_level:.1f} dBFS\n")
    else:
        logger.debug(f"No audio clipping detected. Max peak level: {max_peak_level:.1f} dBFS\n")

    return results


def _characterize_imbalance(abs_diff):
    """Return a human-readable characterization for a given absolute dB difference."""
    if abs_diff < 1.0:
        return "Balanced"
    elif abs_diff < 3.0:
        return "Slight imbalance"
    elif abs_diff < 6.0:
        return "Moderate imbalance"
    else:
        return "Significant imbalance"


def _write_imbalance_results(report_directory, total_audio_frames, channel_rms_values):
    """Compute channel imbalance from per-channel RMS levels and write results to CSV.

    Args:
        report_directory (str): Path to report CSV directory.
        total_audio_frames (int): Total audio frames encountered.
        channel_rms_values (dict): {channel_num: [rms_values...]} for each channel found.
    """
    num_channels = len(channel_rms_values)

    if num_channels == 0:
        logger.warning("No per-channel RMS level data found in QCTools report for channel imbalance analysis\n")
        return None

    sorted_channels = sorted(channel_rms_values.keys())

    if num_channels == 1:
        # Mono — no imbalance to compute, but report the single channel's mean RMS
        ch = sorted_channels[0]
        vals = channel_rms_values[ch]
        mean_rms = sum(vals) / len(vals)
        results = {
            'total_audio_frames': total_audio_frames,
            'num_channels': 1,
            'channel_means': {ch: mean_rms},
            'pairwise': [],
            'overall_characterization': "Mono (single channel)",
        }
        imbalance_csv = os.path.join(report_directory, "qct-parse_channel_imbalance.csv")
        with open(imbalance_csv, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Channel Imbalance Analysis Results"])
            writer.writerow(["Total Audio Frames", total_audio_frames])
            writer.writerow(["Number of Channels", 1])
            writer.writerow([f"Channel {ch} Mean RMS (dBFS)", f"{mean_rms:.1f}"])
            writer.writerow(["Overall Characterization", "Mono (single channel)"])
        logger.debug(f"Channel imbalance analysis: Mono — single channel detected (Ch{ch}: {mean_rms:.1f} dBFS)\n")
        return results

    # Multi-channel: compute per-channel means and pairwise comparisons
    frames_with_all = min(len(channel_rms_values[ch]) for ch in sorted_channels)
    if frames_with_all == 0:
        logger.warning("No per-channel RMS level data found in QCTools report for channel imbalance analysis\n")
        return None

    channel_means = {}
    for ch in sorted_channels:
        vals = channel_rms_values[ch][:frames_with_all]
        channel_means[ch] = sum(vals) / len(vals)

    # Pairwise comparisons between all channel pairs
    pairwise = []
    for i, ch_a in enumerate(sorted_channels):
        for ch_b in sorted_channels[i + 1:]:
            mean_diff = channel_means[ch_a] - channel_means[ch_b]
            abs_diff = abs(mean_diff)
            characterization = _characterize_imbalance(abs_diff)
            if abs_diff >= 1.0:
                louder = f"Channel {ch_a}" if mean_diff > 0 else f"Channel {ch_b}"
            else:
                louder = "Neither"
            pairwise.append({
                'channel_a': ch_a,
                'channel_b': ch_b,
                'mean_difference_db': mean_diff,
                'abs_mean_difference_db': abs_diff,
                'characterization': characterization,
                'louder_channel': louder,
            })

    # Overall characterization: worst pairwise result
    worst = max(pairwise, key=lambda p: p['abs_mean_difference_db'])
    overall_characterization = worst['characterization']

    # Silent channel detection — only when significant imbalance is found
    silent_channels = []
    if overall_characterization == "Significant imbalance":
        for ch in sorted_channels:
            mean_rms = channel_means[ch]
            if mean_rms == float('-inf') or mean_rms < SILENCE_THRESHOLD_DB:
                silent_channels.append(ch)

    results = {
        'total_audio_frames': total_audio_frames,
        'num_channels': num_channels,
        'frames_analyzed': frames_with_all,
        'channel_means': channel_means,
        'pairwise': pairwise,
        'overall_characterization': overall_characterization,
        'silent_channels': silent_channels,
    }

    # Write CSV
    imbalance_csv = os.path.join(report_directory, "qct-parse_channel_imbalance.csv")
    with open(imbalance_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Channel Imbalance Analysis Results"])
        writer.writerow(["Total Audio Frames", total_audio_frames])
        writer.writerow(["Number of Channels", num_channels])
        writer.writerow(["Frames Analyzed", frames_with_all])
        for ch in sorted_channels:
            writer.writerow([f"Channel {ch} Mean RMS (dBFS)", f"{channel_means[ch]:.1f}"])
        writer.writerow([])
        if num_channels == 2:
            # For stereo, keep a simple summary like before
            p = pairwise[0]
            writer.writerow(["Mean Difference (dB)", f"{p['mean_difference_db']:+.1f}"])
            writer.writerow(["Characterization", p['characterization']])
            writer.writerow(["Louder Channel", p['louder_channel']])
        else:
            # For >2 channels, write pairwise comparison table
            writer.writerow(["Pairwise Comparisons"])
            writer.writerow(["Channel A", "Channel B", "Mean Difference (dB)", "Characterization", "Louder Channel"])
            for p in pairwise:
                writer.writerow([
                    f"Channel {p['channel_a']}", f"Channel {p['channel_b']}",
                    f"{p['mean_difference_db']:+.1f}", p['characterization'], p['louder_channel']
                ])
        writer.writerow([])
        writer.writerow(["Overall Characterization", overall_characterization])
        if silent_channels:
            writer.writerow(["Silent Channels", ", ".join(f"Channel {ch}" for ch in silent_channels)])

    # Log summary
    ch_summary = ", ".join(f"Ch{ch}: {channel_means[ch]:.1f} dBFS" for ch in sorted_channels)
    if silent_channels:
        silent_str = ", ".join(f"Ch{ch}" for ch in silent_channels)
        logger.warning(f"Channel imbalance analysis: {overall_characterization} — silent channel(s) detected: {silent_str} ({ch_summary})\n")
    else:
        logger.debug(f"Channel imbalance analysis: {overall_characterization} ({ch_summary})\n")

    return results


# ---------------------------------------------------------------------------
# Identical channel (dual mono) detection
# ---------------------------------------------------------------------------
#
# Two-stage. The screen rides the audio parse that is already happening: on
# every frame with audio, each channel pair is compared on its astats RMS
# level and (for stereo) the aphasemeter phase correlation. Duplicated
# channels agree on both, frame after frame. Because matching levels alone
# are only circumstantial, a pair that screens as a match is then confirmed
# by decoding the two channels and comparing them sample by sample — that is
# what lets the report say "bit-identical" rather than "looks the same".
# Comparing both the difference (A-B) and the sum (A+B) separates a straight
# duplicate from a polarity-inverted one, which the astats metrics cannot
# tell apart.


def _identical_new_pair():
    """Per-pair screening state."""
    return {
        'active_frames': 0,
        'silent_frames': 0,
        'matching_frames': 0,
        'inverted_frames': 0,
        'phase_sum': 0.0,
        'phase_frames': 0,
        'run_start': None,
        'run_end': None,
        'spans': [],
    }


def _identical_close_run(pair):
    """Close the pair's open matching run, keeping it if it is long enough."""
    start, end = pair['run_start'], pair['run_end']
    pair['run_start'] = None
    pair['run_end'] = None
    if start is None or end is None:
        return
    if end - start >= IDENTICAL_MIN_SPAN_SEC:
        pair['spans'].append((start, end))


def _identical_update_pairs(pairs, frame_time, frame_channel_rms, frame_phase):
    """Screen one audio frame: does each channel pair look duplicated here?

    Frames where every channel is at or below the silence floor are skipped —
    silence matches silence trivially, and a silent gap in the middle of a
    duplicated passage shouldn't break the matching run either.
    """
    channels = sorted(frame_channel_rms)
    # aphasemeter measures one stereo pair, so its phase value only describes
    # the channels of a 2-channel file.
    use_phase = len(channels) == 2 and frame_phase is not None

    for i, ch_a in enumerate(channels):
        for ch_b in channels[i + 1:]:
            pair = pairs.setdefault((ch_a, ch_b), _identical_new_pair())
            rms_a = frame_channel_rms[ch_a]
            rms_b = frame_channel_rms[ch_b]

            if max(rms_a, rms_b) <= IDENTICAL_SILENCE_FLOOR_DB:
                pair['silent_frames'] += 1
                continue

            pair['active_frames'] += 1
            if use_phase:
                pair['phase_sum'] += frame_phase
                pair['phase_frames'] += 1

            level_match = abs(rms_a - rms_b) <= IDENTICAL_RMS_TOLERANCE_DB
            # Phase near +1 is a duplicate, near -1 a polarity-inverted one;
            # both are "the same audio twice" as far as the screen cares.
            phase_match = not use_phase or abs(frame_phase) >= IDENTICAL_PHASE_THRESHOLD

            if level_match and phase_match:
                pair['matching_frames'] += 1
                if use_phase and frame_phase < 0:
                    pair['inverted_frames'] += 1
                if pair['run_start'] is None:
                    pair['run_start'] = frame_time
                pair['run_end'] = frame_time
            else:
                _identical_close_run(pair)


def _identical_db(amplitude):
    """Convert a linear full-scale amplitude to dBFS (-inf for digital zero)."""
    if amplitude <= 0:
        return float('-inf')
    return 20 * math.log10(amplitude)


def _identical_db_str(value):
    """Format a dBFS value for CSV output."""
    if value is None:
        return "N/A"
    if value == float('-inf'):
        return "-inf"
    return f"{value:.1f}"


def _measure_channel_difference(video_path, ch_a, ch_b, audio_stream_indexes=None,
                                start=None, duration=None, check_cancelled=None):
    """Compare two channels sample by sample by decoding them through ffmpeg.

    The two channels are panned into a stereo pipe (for multi-stream sources
    the streams are amerged first, in the same order the audio stats sidecar
    used, so channel N is stream N here too) and read as float samples. No
    resampling is applied — the comparison has to see the original samples.

    Returns a dict of peak/RMS levels for the channels, their difference and
    their sum, or None if the decode failed.
    """
    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", video_path]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]

    pan = f"pan=stereo|c0=c{ch_a - 1}|c1=c{ch_b - 1}"
    if audio_stream_indexes and len(audio_stream_indexes) > 1:
        inputs = "".join(f"[0:a:{idx}]" for idx in audio_stream_indexes)
        graph = f"{inputs}amerge=inputs={len(audio_stream_indexes)},{pan}[cmp]"
        cmd += ["-filter_complex", graph, "-map", "[cmp]"]
    else:
        cmd += ["-map", "0:a:0", "-filter:a", pan]
    cmd += ["-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning(f"Identical-channel detection: ffmpeg invocation failed: {exc}")
        return None

    chunk_bytes = _IDENTICAL_CHUNK_SAMPLES * 2 * 4
    samples = 0
    peak_a = peak_b = peak_diff = peak_sum = 0.0
    sumsq_diff = sumsq_sum = 0.0
    cancelled = False
    try:
        while True:
            if check_cancelled and check_cancelled():
                cancelled = True
                proc.kill()
                break
            buf = proc.stdout.read(chunk_bytes)
            if not buf:
                break
            usable = len(buf) - (len(buf) % 8)
            if usable == 0:
                break
            block = np.frombuffer(buf[:usable], dtype=np.float32).reshape(-1, 2).astype(np.float64)
            a = block[:, 0]
            b = block[:, 1]
            diff = a - b
            total = a + b
            samples += block.shape[0]
            peak_a = max(peak_a, float(np.max(np.abs(a))))
            peak_b = max(peak_b, float(np.max(np.abs(b))))
            peak_diff = max(peak_diff, float(np.max(np.abs(diff))))
            peak_sum = max(peak_sum, float(np.max(np.abs(total))))
            sumsq_diff += float(np.sum(diff ** 2))
            sumsq_sum += float(np.sum(total ** 2))
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode('utf-8', 'replace').strip()
        proc.stderr.close()
        proc.wait()

    if cancelled:
        return None
    if samples == 0:
        logger.warning(
            f"Identical-channel detection: no audio decoded for channels {ch_a}/{ch_b}"
            f"{': ' + stderr if stderr else ''}"
        )
        return None
    if proc.returncode != 0:
        logger.warning(f"Identical-channel detection: ffmpeg decode failed: {stderr}")
        return None

    return {
        'samples': samples,
        'peak_a_db': _identical_db(peak_a),
        'peak_b_db': _identical_db(peak_b),
        'program_peak_db': _identical_db(max(peak_a, peak_b)),
        'difference_peak_db': _identical_db(peak_diff),
        'difference_rms_db': _identical_db(math.sqrt(sumsq_diff / samples)),
        'sum_peak_db': _identical_db(peak_sum),
        'sum_rms_db': _identical_db(math.sqrt(sumsq_sum / samples)),
    }


def _characterize_identical(measurement, inverted_screen):
    """Turn a sample-level measurement into a characterization.

    Compares the residual (difference for a straight duplicate, sum for a
    polarity-inverted one) against digital silence first, then against the
    program's own peak — a residual 40 dB down is the same audio twice with
    only rounding or a hair of level difference between the copies.
    """
    diff_peak = measurement['difference_peak_db']
    sum_peak = measurement['sum_peak_db']
    program_peak = measurement['program_peak_db']

    inverted = sum_peak < diff_peak if inverted_screen else False
    residual = sum_peak if inverted else diff_peak

    if residual <= IDENTICAL_BIT_IDENTICAL_PEAK_DB:
        base = "Bit-identical"
    elif program_peak != float('-inf') and residual <= program_peak - IDENTICAL_EFFECTIVE_MARGIN_DB:
        base = "Effectively identical"
    else:
        return "Distinct channels", inverted, residual

    if inverted:
        base = f"Polarity-inverted duplicate ({base.lower()})"
    return base, inverted, residual


def _detect_and_write_identical_results(identical_pairs, report_directory, total_audio_frames,
                                        video_path=None, audio_stream_indexes=None,
                                        fps=None, tc_start_frames=0, tc_drop_frame=False,
                                        check_cancelled=None):
    """Screen every channel pair, confirm the candidates, and write the CSV.

    Returns the results dict, or None when there is nothing to compare (a mono
    file, or a report with no per-channel audio stats).
    """
    if not identical_pairs:
        logger.debug(
            "Identical-channel detection: fewer than two audio channels to compare\n"
        )
        return None

    pair_results = []
    for (ch_a, ch_b), pair in sorted(identical_pairs.items()):
        _identical_close_run(pair)
        active = pair['active_frames']
        matching = pair['matching_frames']
        fraction = matching / active if active else 0.0
        mean_phase = (pair['phase_sum'] / pair['phase_frames']
                      if pair['phase_frames'] else None)
        spans = sorted(pair['spans'], key=lambda s: s[1] - s[0], reverse=True)

        if active < IDENTICAL_MIN_ACTIVE_FRAMES:
            screen = 'insufficient'
        elif fraction >= IDENTICAL_FULL_MATCH_FRACTION:
            screen = 'full'
        elif fraction >= IDENTICAL_PARTIAL_MATCH_FRACTION and spans:
            screen = 'partial'
        else:
            screen = 'distinct'

        pair_results.append({
            'channel_a': ch_a,
            'channel_b': ch_b,
            'active_frames': active,
            'silent_frames': pair['silent_frames'],
            'matching_frames': matching,
            'match_fraction': fraction,
            'mean_phase': mean_phase,
            'inverted_screen': pair['inverted_frames'] > matching / 2 if matching else False,
            'spans': spans,
            'screen': screen,
            'measurement': None,
            'compared_region': None,
            'characterization': (
                "Insufficient audio" if screen == 'insufficient' else "Distinct channels"
            ),
        })

    # Confirm the most convincing candidates by comparing actual samples.
    candidates = [p for p in pair_results if p['screen'] in ('full', 'partial')]
    candidates.sort(key=lambda p: p['match_fraction'], reverse=True)
    for pair in candidates[:IDENTICAL_MAX_CONFIRM_PAIRS]:
        if pair['screen'] == 'full':
            start = duration = None
            region_label = "Whole file"
        else:
            span_start, span_end = pair['spans'][0]
            start, duration = span_start, span_end - span_start
            region_label = (
                f"{_tc_format_timecode(span_start, fps, tc_start_frames, tc_drop_frame)}"
                f"–{_tc_format_timecode(span_end, fps, tc_start_frames, tc_drop_frame)}"
            )

        measurement = None
        if video_path:
            measurement = _measure_channel_difference(
                video_path, pair['channel_a'], pair['channel_b'],
                audio_stream_indexes=audio_stream_indexes,
                start=start, duration=duration, check_cancelled=check_cancelled
            )

        if measurement is None:
            # No sample-level proof available — report what the metrics show,
            # marked as unconfirmed rather than asserting the channels match.
            pair['compared_region'] = "Not compared"
            pair['characterization'] = (
                "Polarity-inverted duplicate (unconfirmed)" if pair['inverted_screen']
                else "Effectively identical (unconfirmed)"
            )
            continue

        pair['measurement'] = measurement
        pair['compared_region'] = region_label
        characterization, inverted, residual = _characterize_identical(
            measurement, pair['inverted_screen']
        )
        pair['inverted'] = inverted
        pair['residual_peak_db'] = residual
        if characterization == "Distinct channels":
            # The screen's matching levels weren't backed by the samples.
            pair['characterization'] = "Distinct channels"
            pair['screen'] = 'distinct'
        elif pair['screen'] == 'partial':
            pair['characterization'] = f"Partially identical ({characterization.lower()})"
        else:
            pair['characterization'] = characterization

    # Overall verdict — the strongest pair result wins.
    identical_pairs_found = [p for p in pair_results if p['screen'] == 'full'
                             and p['characterization'] != "Distinct channels"]
    partial_pairs_found = [p for p in pair_results if p['screen'] == 'partial'
                           and p['characterization'] != "Distinct channels"]
    if identical_pairs_found:
        strongest = identical_pairs_found[0]
        if strongest.get('inverted'):
            overall = "Polarity-inverted duplicate channels"
        else:
            overall = "Identical channels (dual mono)"
    elif partial_pairs_found:
        strongest = partial_pairs_found[0]
        overall = "Partially identical channels"
    elif all(p['screen'] == 'insufficient' for p in pair_results):
        strongest = pair_results[0]
        overall = "Insufficient audio to compare"
    else:
        strongest = max(pair_results, key=lambda p: p['match_fraction'])
        overall = "Distinct channels"

    results = {
        'total_audio_frames': total_audio_frames,
        'num_channels': len({ch for p in pair_results for ch in (p['channel_a'], p['channel_b'])}),
        'overall_verdict': overall,
        'overall_characterization': strongest['characterization'],
        'identical_channel_pairs': [
            (p['channel_a'], p['channel_b']) for p in identical_pairs_found
        ],
        'pairs': pair_results,
    }

    _write_identical_results_csv(
        report_directory, results, fps=fps,
        tc_start_frames=tc_start_frames, tc_drop_frame=tc_drop_frame
    )

    pair_summary = ", ".join(
        f"Ch{p['channel_a']}/Ch{p['channel_b']}: {p['characterization']} "
        f"({p['match_fraction'] * 100:.1f}% of active frames)"
        for p in pair_results
    )
    if identical_pairs_found or partial_pairs_found:
        logger.warning(f"Identical channel detection: {overall} — {pair_summary}\n")
    else:
        logger.debug(f"Identical channel detection: {overall} — {pair_summary}\n")

    return results


def _write_identical_results_csv(report_directory, results, fps=None,
                                 tc_start_frames=0, tc_drop_frame=False):
    """Write identical-channel results to qct-parse_identical_channels.csv."""
    identical_csv = os.path.join(report_directory, "qct-parse_identical_channels.csv")
    with open(identical_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Identical Channel Detection Results"])
        writer.writerow(["Total Audio Frames", results['total_audio_frames']])
        writer.writerow(["Number of Channels", results['num_channels']])
        writer.writerow(["Overall Verdict", results['overall_verdict']])
        writer.writerow(["Overall Characterization", results['overall_characterization']])
        writer.writerow([])

        writer.writerow(["Channel Pair Comparisons"])
        writer.writerow([
            "Channel A", "Channel B", "Active Frames", "Matching Frames", "Matching (%)",
            "Mean Phase Correlation", "Difference Peak (dBFS)", "Sum Peak (dBFS)",
            "Program Peak (dBFS)", "Compared Region", "Characterization"
        ])
        for p in results['pairs']:
            m = p['measurement'] or {}
            phase = p['mean_phase']
            writer.writerow([
                f"Channel {p['channel_a']}",
                f"Channel {p['channel_b']}",
                p['active_frames'],
                p['matching_frames'],
                f"{p['match_fraction'] * 100:.2f}",
                f"{phase:.3f}" if phase is not None else "N/A",
                _identical_db_str(m.get('difference_peak_db')),
                _identical_db_str(m.get('sum_peak_db')),
                _identical_db_str(m.get('program_peak_db')),
                p['compared_region'] or "Not compared",
                p['characterization'],
            ])

        matching_regions = [
            (p, span) for p in results['pairs'] for span in p['spans']
            if p['characterization'] not in ("Distinct channels", "Insufficient audio")
        ]
        if matching_regions:
            writer.writerow([])
            writer.writerow(["Matching Regions"])
            writer.writerow(["Channel A", "Channel B", "Start", "End", "Duration (s)"])
            for p, (span_start, span_end) in sorted(
                matching_regions, key=lambda item: item[1][0]
            ):
                writer.writerow([
                    f"Channel {p['channel_a']}",
                    f"Channel {p['channel_b']}",
                    _tc_format_timecode(span_start, fps, tc_start_frames, tc_drop_frame),
                    _tc_format_timecode(span_end, fps, tc_start_frames, tc_drop_frame),
                    f"{span_end - span_start:.1f}",
                ])

    return identical_csv


# ---------------------------------------------------------------------------
# Audible timecode detection
# ---------------------------------------------------------------------------

def _tc_mean(values):
    if not values:
        return float('nan')
    return sum(values) / len(values)


def _tc_median(values):
    if not values:
        return float('nan')
    s = sorted(values)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) / 2
    return s[n // 2]


def _tc_stdev(values):
    if len(values) < 2:
        return 0.0
    m = _tc_mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def _tc_format_time(seconds):
    """Format a duration in seconds as HH:MM:SS.s (an elapsed length, not a
    timecode position)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f'{h:d}:{m:02d}:{s:04.1f}'
    return f'{m:d}:{s:04.1f}'


_NTSC_FPS = 30000 / 1001  # 29.97


def _tc_drop_count(nominal):
    """Frames dropped per minute for drop-frame timecode at this nominal rate
    (2 at 30 fps, 4 at 60 fps)."""
    return nominal // 15


def _tc_df_to_frames(h, m, s, f, nominal):
    """Drop-frame timecode fields → absolute frame index."""
    drop = _tc_drop_count(nominal)
    total_minutes = 60 * h + m
    base = (((h * 60 + m) * 60 + s) * nominal) + f
    return base - drop * (total_minutes - total_minutes // 10)


def _tc_frames_to_df(frame, nominal):
    """Absolute frame index → drop-frame timecode fields (h, m, s, f)."""
    drop = _tc_drop_count(nominal)
    frames_per_10min = nominal * 600 - drop * 9
    frames_per_min = nominal * 60 - drop
    d, mod = divmod(frame, frames_per_10min)
    if mod > drop:
        frame += drop * 9 * d + drop * ((mod - drop) // frames_per_min)
    else:
        frame += drop * 9 * d
    f = frame % nominal
    s = (frame // nominal) % 60
    m = (frame // (nominal * 60)) % 60
    h = (frame // (nominal * 3600)) % 24
    return h, m, s, f


def _tc_parse_start_timecode(tc_string, nominal):
    """Parse an ffprobe TIMECODE tag ('HH:MM:SS:FF' NDF / 'HH:MM:SS;FF' DF) into
    (start_frame_offset, drop_frame). Returns (0, False) on anything unparseable."""
    if not tc_string:
        return 0, False
    tc = tc_string.strip()
    drop = ';' in tc          # SMPTE uses ';' before the frames field for DF
    parts = re.split(r'[:;]', tc)
    if len(parts) != 4:
        return 0, False
    try:
        h, m, s, f = (int(p) for p in parts)
    except ValueError:
        return 0, False
    if drop:
        return _tc_df_to_frames(h, m, s, f, nominal), True
    return (((h * 60 + m) * 60 + s) * nominal) + f, False


def _tc_format_timecode(seconds, fps=None, start_frames=0, drop_frame=False):
    """Format a real-elapsed-seconds position as a timecode HH:MM:SS:FF (NDF) or
    HH:MM:SS;FF (DF) at the given frame rate, offset by the stream's start
    timecode.

    QCTools timestamps are real wall-clock seconds; an NLE displays the file's
    embedded timecode. NDF labels every frame at the nominal integer rate (30 for
    29.97), so it drifts ~0.1% behind wall time (~3.6 s/hour); DF skips frame
    *numbers* to stay aligned with wall time. Converting the real-time position
    to a frame index, adding the stream's start-TC offset, and re-labelling in
    the file's own convention makes the reported position match the NLE.
    """
    if not fps or fps <= 0:
        fps = _NTSC_FPS
    nominal = max(1, int(round(fps)))
    total = start_frames + int(round(seconds * fps))
    if total < 0:
        total = 0
    # Drop-frame is only defined for fractional NTSC-family rates (29.97/59.94).
    if drop_frame and abs(nominal - fps) > 1e-6:
        h, m, s, f = _tc_frames_to_df(total, nominal)
        return f'{h:02d}:{m:02d}:{s:02d};{f:02d}'
    f = total % nominal
    ts = total // nominal
    s = ts % 60
    m = (ts // 60) % 60
    h = ts // 3600
    return f'{h:02d}:{m:02d}:{s:02d}:{f:02d}'


def _tc_filter_by_consecutive(detections, min_consecutive):
    """Keep only detections that are part of a consecutive run of sufficient length."""
    if len(detections) < min_consecutive:
        return []

    by_crit = defaultdict(list)
    for d in detections:
        by_crit[d.criterion].append(d)

    result = []
    for crit, dets in by_crit.items():
        dets.sort(key=lambda d: d.start_time)
        run = [dets[0]]
        for d in dets[1:]:
            gap = d.start_time - run[-1].end_time
            if gap <= _TC_R128_WINDOW_SEC * 1.5:
                run.append(d)
            else:
                if len(run) >= min_consecutive:
                    result.extend(run)
                run = [d]
        if len(run) >= min_consecutive:
            result.extend(run)

    return result


def _tc_merge_detections(detections):
    """Merge overlapping detections with the same criterion into spans."""
    if not detections:
        return []

    by_crit = defaultdict(list)
    for d in detections:
        by_crit[d.criterion].append(d)

    merged = []
    for crit, dets in by_crit.items():
        dets.sort(key=lambda d: d.start_time)
        current = _TCDetection(
            start_time=dets[0].start_time,
            end_time=dets[0].end_time,
            criterion=dets[0].criterion,
            channel=dets[0].channel,
            confidence=dets[0].confidence,
            details=dets[0].details
        )
        for d in dets[1:]:
            if d.start_time <= current.end_time + _TC_R128_WINDOW_SEC:
                current.end_time = max(current.end_time, d.end_time)
                if d.confidence == 'high':
                    current.confidence = 'high'
            else:
                merged.append(current)
                current = _TCDetection(
                    start_time=d.start_time,
                    end_time=d.end_time,
                    criterion=d.criterion,
                    channel=d.channel,
                    confidence=d.confidence,
                    details=d.details
                )
        merged.append(current)

    merged.sort(key=lambda d: d.start_time)
    return merged


# Cross-method consensus. The per-method detectors (R128 criteria A/B/C and
# per-channel astats) each emit their own regions, and the same stretch of
# audible timecode typically lights up several of them at once — so the raw
# detection list is full of overlapping near-duplicates that differ only by
# which method fired. The consensus collapses them into one row per region.
#
# astats is privileged for segmentation. It measures each channel
# independently, so it resolves the true region boundaries — including spots
# where the TC carrier briefly stutters or one channel drops out. R128
# measures only the channel *mix*, so a single loud channel keeps its meter
# pinned and it smooths over gaps astats correctly reports. So astats region
# boundaries are taken as authoritative; R128 is demoted to corroboration: it
# adds "R128" to a region's method list where it overlaps.
#
# When per-channel astats data exists in the report, R128 is corroboration
# *only* — an R128 span that astats never confirmed is discarded, because the
# R128 loudness-statistics gates alone cannot tell LTC from other
# steady-loudness audio (heavily compressed music passes criterion A; quiet
# speech over a near-silent second channel passes criterion C), whereas real
# LTC reliably trips the per-channel astats fingerprint. Standalone R128
# regions are only trusted on older QCTools reports that carry no per-channel
# astats measurements, where R128 is the only evidence available.
_TC_CONF_RANK = {'high': 3, 'medium': 2, 'low': 1}


@dataclass
class _TCConsensusRegion:
    """A consensus region built by merging overlapping per-method detections."""
    start_time: float = 0.0
    end_time: float = 0.0
    channel: str = ""
    confidence: str = ""
    methods: str = ""
    method_count: int = 0


def _tc_base_method(criterion):
    """Collapse a per-method criterion to its detector family for corroboration
    counting: every 'R128-*' criterion is the R128 detector; 'astats (chN)'
    stays per-channel so a both-channels region still reads as two methods."""
    c = (criterion or "").strip()
    if c.lower().startswith('r128'):
        return 'R128'
    if c.lower().startswith('astats'):
        return c
    return c or 'unknown'


def _tc_consensus_channel(dets):
    """Human-readable 'which channel carries TC' label for a consensus region,
    derived from the per-channel astats detections that contributed (R128 rows
    are mix-based and name no channel). Same vocabulary as the report's
    top-level channel summary."""
    chans = set()
    saw_both = False
    for d in dets:
        ch = (d.channel or "").strip().lower()
        if "both" in ch:
            saw_both = True
        for num in re.findall(r'ch(\d+)', ch):
            chans.add(num)
    if saw_both or len(chans) > 1:
        return "Both channels"
    if len(chans) == 1:
        return f"Channel {next(iter(chans))}"
    return "Not channel-specific (mix-based)"


def _tc_overlaps(a_start, a_end, b_start, b_end):
    """True if spans [a_start, a_end] and [b_start, b_end] overlap or touch."""
    return a_start <= b_end and b_start <= a_end


def _tc_build_consensus(detections, astats_available=False):
    """Build consensus regions from the per-method detections, privileging
    astats for segmentation. The incoming astats detections are already
    per-channel-merged, so their boundaries are the authoritative region
    boundaries — overlapping channel-1/channel-2 spans are unioned into one
    region, but a gap astats left between two same-channel spans is preserved.
    R128 detections never create or extend a boundary; they only add the "R128"
    method label where they overlap an astats region. An R128 span astats never
    detected stands alone only when astats_available is False (the report has
    no per-channel astats data, so R128 is the only evidence); with astats data
    present, an uncorroborated R128 span is discarded as a false positive.
    Returns _TCConsensusRegion list sorted by start time."""
    if not detections:
        return []

    astats = [d for d in detections if _tc_base_method(d.criterion) != 'R128']
    r128 = [d for d in detections if _tc_base_method(d.criterion) == 'R128']

    # Group astats detections that overlap (across channels) into regions.
    # Same-channel detections arrive pre-merged and non-overlapping, so a gap
    # the detector deliberately left survives here as a region boundary.
    regions = []
    for d in sorted(astats, key=lambda d: d.start_time):
        if regions and d.start_time <= regions[-1]['end']:
            regions[-1]['end'] = max(regions[-1]['end'], d.end_time)
            regions[-1]['dets'].append(d)
        else:
            regions.append({'start': d.start_time, 'end': d.end_time, 'dets': [d]})

    # R128-only spans: an R128 detection that overlaps no astats region becomes
    # its own region, but only when per-channel astats data was unavailable —
    # if astats measured the channels and found no TC fingerprint there, the
    # R128 loudness statistics alone are not trusted (stable music and quiet
    # speech both pass them).
    for d in sorted(r128, key=lambda d: d.start_time):
        if not any(_tc_overlaps(d.start_time, d.end_time, r['start'], r['end'])
                   for r in regions):
            if astats_available:
                logger.debug(
                    f"Discarding uncorroborated R128 timecode detection "
                    f"{d.start_time:.1f}-{d.end_time:.1f}s ({d.criterion}): "
                    f"per-channel astats data shows no TC fingerprint\n"
                )
            else:
                regions.append({'start': d.start_time, 'end': d.end_time, 'dets': [d]})

    regions.sort(key=lambda r: r['start'])

    out = []
    for r in regions:
        overlapping_r128 = [
            d for d in r128 if _tc_overlaps(d.start_time, d.end_time, r['start'], r['end'])
        ]
        astats_methods = sorted({
            _tc_base_method(d.criterion) for d in r['dets']
            if _tc_base_method(d.criterion) != 'R128'
        })
        has_r128 = overlapping_r128 or any(
            _tc_base_method(d.criterion) == 'R128' for d in r['dets']
        )
        methods = (['R128'] if has_r128 else []) + astats_methods or ['R128']

        confs = [d.confidence for d in r['dets']] + [d.confidence for d in overlapping_r128]
        best_conf = max(
            confs, key=lambda c: _TC_CONF_RANK.get((c or '').lower(), 0)
        ) if confs else ''

        out.append(_TCConsensusRegion(
            start_time=r['start'],
            end_time=r['end'],
            channel=_tc_consensus_channel(r['dets']),
            confidence=best_conf,
            methods=" + ".join(methods),
            method_count=len(methods),
        ))
    return out


# Boundary refinement: window detection is quantized to the 15s step, so the
# reported start can be off by up to one window. Once a region is confirmed,
# walk back per-frame using a smoothed RMS gate to recover the true
# silence-to-TC transition point.
#
# Refinement uses a wider RMS band than detection. The job here is only to
# separate silence (≪ -50 dBFS) from any audible level — early-TC frames can
# briefly dip below -30 dBFS during ramp-up, and a strict band would pull the
# boundary forward by tens of seconds. False positives are prevented upstream
# by the stdev gates on the detection windows.
#
# 1s smoothing is narrower than the silence→TC transition typically takes, so
# the rolling median crosses the boundary cleanly. Larger windows blur the
# transition and the boundary either lags (mean) or overshoots into silence
# (median).
_TC_REFINE_SMOOTH_SEC = 1.0
_TC_REFINE_RMS_MIN = -45.0
_TC_REFINE_RMS_MAX = -8.0


def _tc_refine_start_time(frames, times, region_start_time, get_rms):
    """Walk back from region_start_time to find the earliest frame whose
    smoothed RMS_level still sits in the TC band.

    get_rms(frame) returns the per-frame metric (r128.M for R128, astats RMS
    for astats) or NaN.
    """
    if not frames or not times:
        return region_start_time
    dt = times[1] - times[0] if len(times) >= 2 and times[1] > times[0] else 0.1
    smooth_n = max(1, int(_TC_REFINE_SMOOTH_SEC / dt))

    # Find index of first frame at or after region_start_time
    start_idx = 0
    for j, t in enumerate(times):
        if t >= region_start_time:
            start_idx = j
            break

    best_idx = start_idx
    i = start_idx
    while i > 0:
        i -= 1
        end = min(i + smooth_n, len(frames))
        vals = []
        for j in range(i, end):
            v = get_rms(frames[j])
            if not math.isnan(v):
                vals.append(v)
        if not vals:
            break
        # Median, not mean — silence frames sit ≪ -50 dBFS and would drag the
        # mean across the boundary, letting the walk overshoot into silence.
        med = _tc_median(vals)
        if _TC_REFINE_RMS_MIN <= med <= _TC_REFINE_RMS_MAX:
            best_idx = i
        else:
            break
    return times[best_idx]


def _detect_r128_timecode(frames):
    """Detect audible timecode using EBU R128 measurements."""
    if not frames:
        return []

    times = [f['time'] for f in frames]
    m_vals = [f['tags'].get('lavfi.r128.M', float('nan')) for f in frames]
    s_vals = [f['tags'].get('lavfi.r128.S', float('nan')) for f in frames]
    lra_vals = [f['tags'].get('lavfi.r128.LRA', float('nan')) for f in frames]
    lra_high_vals = [f['tags'].get('lavfi.r128.LRA.high', float('nan')) for f in frames]

    # Get integrated loudness from last valid frame
    i_val = float('nan')
    for frame in reversed(frames):
        candidate = frame['tags'].get('lavfi.r128.I', float('nan'))
        if not math.isnan(candidate) and candidate < -1.0:
            i_val = candidate
            break

    # Determine frame interval
    if len(times) >= 2:
        dt_sec = times[1] - times[0]
    else:
        dt_sec = 0.1
    window_size = max(1, int(_TC_R128_WINDOW_SEC / dt_sec))

    detections = []
    detections.extend(_detect_r128_criterion_a(times, m_vals, lra_vals, window_size))
    detections.extend(_detect_r128_criterion_b(times, m_vals, lra_vals, lra_high_vals, i_val, window_size))
    detections.extend(_detect_r128_criterion_c(times, m_vals, s_vals, lra_high_vals, window_size))

    merged = _tc_merge_detections(detections)

    # Refine each region's start time using per-frame r128.M
    def _get_r128_m(frame):
        return frame['tags'].get('lavfi.r128.M', float('nan'))
    for d in merged:
        d.start_time = _tc_refine_start_time(frames, times, d.start_time, _get_r128_m)

    return merged


def _detect_r128_criterion_a(times, m_vals, lra_vals, window_size):
    """Criterion A: detect a stable mix at TC level via rolling windows.

    R128 measures the channel mix, not individual channels, so this fires
    whenever momentary loudness is steady and at LTC level — which includes
    both-channels-TC and one-channel-TC-plus-quiet-channel scenarios. Use
    astats results to determine which channels actually carry TC.
    """
    detections = []
    for i in range(0, len(times) - window_size + 1, window_size // 2):
        end = min(i + window_size, len(times))
        w_m = [v for v in m_vals[i:end] if not math.isnan(v)]
        w_lra = [v for v in lra_vals[i:end] if not math.isnan(v)]

        if not w_m or not w_lra:
            continue

        m_std = _tc_stdev(w_m)
        m_mean = _tc_mean(w_m)
        lra_med = _tc_median(w_lra)

        if (m_std < _TC_R128_A_M_STDEV_MAX
                and m_mean > _TC_R128_A_M_MEAN_MIN
                and lra_med < _TC_R128_A_LRA_MEDIAN_MAX):
            detections.append(_TCDetection(
                start_time=times[i],
                end_time=times[min(end - 1, len(times) - 1)],
                criterion='R128-A (stable mix at TC level)',
                channel='n/a (mix-based)',
                confidence='high' if m_std < 1.0 else 'medium',
                details=(f'Loudness stability: {m_std:.2f} dB; '
                         f'Avg loudness: {m_mean:.1f} LUFS; '
                         f'Loudness range: {lra_med:.1f} LU')
            ))

    return _tc_filter_by_consecutive(detections, _TC_R128_MIN_WINDOWS)


def _detect_r128_criterion_b(times, m_vals, lra_vals, lra_high_vals, i_val, window_size):
    """Criterion B: TC + silence."""
    detections = []
    for i in range(0, len(times) - window_size + 1, window_size // 2):
        end = min(i + window_size, len(times))
        w_m = [v for v in m_vals[i:end] if not math.isnan(v)]
        w_lra = [v for v in lra_vals[i:end] if not math.isnan(v)]
        w_lra_h = [v for v in lra_high_vals[i:end] if not math.isnan(v)]

        if not w_m or not w_lra_h or not w_lra:
            continue

        lra_h_mean = _tc_mean(w_lra_h)
        lra_h_std = _tc_stdev(w_lra_h)
        m_med = _tc_median(w_m)
        lra_med = _tc_median(w_lra)

        if math.isnan(i_val):
            m_i_diff = 0
        else:
            m_i_diff = abs(m_med - i_val)

        if (_TC_R128_B_LRA_HIGH_MIN <= lra_h_mean <= _TC_R128_B_LRA_HIGH_MAX
                and lra_h_std < _TC_R128_B_LRA_HIGH_STDEV_MAX
                and m_i_diff > _TC_R128_B_M_I_DIFF_MIN
                and lra_med > _TC_R128_B_LRA_MIN):
            detections.append(_TCDetection(
                start_time=times[i],
                end_time=times[min(end - 1, len(times) - 1)],
                criterion='R128-B (TC + silence)',
                channel='one channel',
                confidence='high' if lra_h_std < 0.5 else 'medium',
                details=(f'Loud-side level: {lra_h_mean:.1f} LUFS '
                         f'(stability +/-{lra_h_std:.2f}); '
                         f'Momentary vs integrated gap: {m_i_diff:.1f} LU; '
                         f'Loudness range: {lra_med:.1f} LU')
            ))

    return _tc_filter_by_consecutive(detections, _TC_R128_MIN_WINDOWS)


def _detect_r128_criterion_c(times, m_vals, s_vals, lra_high_vals, window_size):
    """Criterion C: TC + program audio."""
    detections = []
    for i in range(0, len(times) - window_size + 1, window_size // 2):
        end = min(i + window_size, len(times))
        w_m = [v for v in m_vals[i:end] if not math.isnan(v)]
        w_s = [v for v in s_vals[i:end] if not math.isnan(v)]
        w_lra_h = [v for v in lra_high_vals[i:end] if not math.isnan(v)]

        if not w_m or not w_s or not w_lra_h:
            continue

        ms_diffs = [abs(m - s) for m, s in zip(w_m, w_s)]
        ms_diff_med = _tc_median(ms_diffs)
        m_std = _tc_stdev(w_m)
        lra_h_mean = _tc_mean(w_lra_h)
        lra_h_std = _tc_stdev(w_lra_h)
        m_min = min(w_m)

        if (ms_diff_med > _TC_R128_C_MS_DIFF_MEDIAN_MIN
                and m_std > _TC_R128_C_M_STDEV_MIN
                and _TC_R128_C_LRA_HIGH_MIN <= lra_h_mean <= _TC_R128_C_LRA_HIGH_UPPER
                and lra_h_std < _TC_R128_C_LRA_HIGH_STDEV_MAX
                and m_min > _TC_R128_C_M_MIN):
            detections.append(_TCDetection(
                start_time=times[i],
                end_time=times[min(end - 1, len(times) - 1)],
                criterion='R128-C (TC + program audio)',
                channel='one channel',
                confidence='high' if ms_diff_med > 2.0 else 'medium',
                details=(f'Momentary vs short-term gap: {ms_diff_med:.2f} LU; '
                         f'Loudness stability: {m_std:.1f} dB; '
                         f'Loud-side level: {lra_h_mean:.1f} LUFS '
                         f'(stability +/-{lra_h_std:.2f})')
            ))

    return _tc_filter_by_consecutive(detections, _TC_R128_MIN_WINDOWS)


def _tc_astats_channels(frames):
    """Return the set of channel numbers (as strings) that carry per-channel
    astats measurements, probed from the first few frames."""
    channels = set()
    for frame in frames[:5]:
        for key in frame['tags']:
            if key.startswith('lavfi.astats.1.'):
                channels.add('1')
            elif key.startswith('lavfi.astats.2.'):
                channels.add('2')
    return channels


def _detect_astats_timecode(frames):
    """Detect audible timecode using FFmpeg astats per-channel measurements."""
    if not frames:
        return []

    channels = _tc_astats_channels(frames)
    if not channels:
        return []

    times = [f['time'] for f in frames]
    detections = []

    for ch in sorted(channels):
        detections.extend(_detect_astats_channel_tc(frames, times, ch))

    # Label dual-channel if both channels flagged at the same time
    ch1 = [d for d in detections if d.channel == 'ch1']
    ch2 = [d for d in detections if d.channel == 'ch2']
    for d1 in ch1:
        for d2 in ch2:
            if d1.start_time <= d2.end_time and d2.start_time <= d1.end_time:
                d1.channel = 'both (ch1)'
                d2.channel = 'both (ch2)'

    merged = _tc_merge_detections(detections)

    # Re-derive boundaries per frame: reclaim TC the coarse 30s window dropped
    # around brief interruptions and split each region on sustained per-frame
    # gaps, pinning boundaries to the frame.
    return _tc_refine_astats_gaps(frames, merged)


def _tc_frame_in_band(tags, channel):
    """Per-frame LTC fingerprint test for one channel: True only when all four
    astats metrics sit in their TC bands (the same bands the windowed detector
    uses for its means)."""
    p = f'lavfi.astats.{channel}.'
    rms = tags.get(p + 'RMS_level', float('nan'))
    cf = tags.get(p + 'Crest_factor', float('nan'))
    zcr = tags.get(p + 'Zero_crossings_rate', float('nan'))
    ent = tags.get(p + 'Entropy', float('nan'))
    if any(math.isnan(v) for v in (rms, cf, zcr, ent)):
        return False
    return (_TC_ASTATS_RMS_LEVEL_MIN <= rms <= _TC_ASTATS_RMS_LEVEL_MAX
            and _TC_ASTATS_CREST_FACTOR_MIN <= cf <= _TC_ASTATS_CREST_FACTOR_MAX
            and _TC_ASTATS_ZERO_CROSSINGS_RATE_MIN <= zcr <= _TC_ASTATS_ZERO_CROSSINGS_RATE_MAX
            and _TC_ASTATS_ENTROPY_MIN <= ent <= _TC_ASTATS_ENTROPY_MAX)


def _tc_find_tc_subregions(frames, channel, span_start, span_end):
    """Within [span_start, span_end], classify each frame by the per-frame TC
    fingerprint, find sustained non-TC gaps, and return the TC sub-regions
    (start, end) between them — each trimmed to its first/last TC frame."""
    seg = [f for f in frames if span_start <= f['time'] <= span_end]
    if not seg:
        return []

    raw = [_tc_frame_in_band(f['tags'], channel) for f in seg]
    n = len(seg)
    k = _TC_GAP_REFINE_SMOOTH_FRAMES
    smoothed = []
    for i in range(n):
        window = raw[max(0, i - k):min(n, i + k + 1)]
        smoothed.append(sum(window) > len(window) / 2)

    # contiguous non-TC runs
    gaps = []
    i = 0
    while i < n:
        if not smoothed[i]:
            j = i
            while j < n and not smoothed[j]:
                j += 1
            gaps.append([seg[i]['time'], seg[j - 1]['time']])
            i = j
        else:
            i += 1

    # merge runs split only by a brief TC flicker, then keep the sustained ones
    merged = []
    for g in gaps:
        if merged and g[0] - merged[-1][1] < _TC_GAP_MERGE_SEC:
            merged[-1][1] = g[1]
        else:
            merged.append(g)
    real_gaps = [g for g in merged if (g[1] - g[0]) >= _TC_GAP_MIN_SEC]

    # TC sub-regions = span minus real gaps; trim each to its actual TC frames
    bounds = [span_start] + [t for g in real_gaps for t in g] + [span_end]
    subs = []
    for a, b in zip(bounds[0::2], bounds[1::2]):
        tc_times = [f['time'] for f in seg
                    if a <= f['time'] <= b and _tc_frame_in_band(f['tags'], channel)]
        if tc_times and (tc_times[-1] - tc_times[0]) >= _TC_GAP_MIN_SEC:
            subs.append((tc_times[0], tc_times[-1]))
    return subs


def _tc_refine_astats_gaps(frames, regions):
    """Re-derive astats region boundaries from per-frame data. Bridges regions
    separated by a spurious windowed gap, then splits each bridged span on
    sustained per-frame TC gaps. This recovers TC the coarse 30s stability gate
    discarded around a brief interruption and pins gap boundaries to the frame.
    Operates per channel (criterion 'astats (chN)'); non-astats regions pass
    through untouched."""
    if not frames or not regions:
        return regions

    file_start = frames[0]['time']
    file_end = frames[-1]['time']

    by_crit = defaultdict(list)
    for d in regions:
        by_crit[d.criterion].append(d)

    refined = []
    for crit, dets in by_crit.items():
        try:
            channel = crit.split('ch')[1].split(')')[0]
        except IndexError:
            refined.extend(dets)
            continue

        dets.sort(key=lambda d: d.start_time)
        groups = [[dets[0]]]
        for d in dets[1:]:
            if d.start_time - max(g.end_time for g in groups[-1]) <= _TC_GAP_BRIDGE_SEC:
                groups[-1].append(d)
            else:
                groups.append([d])

        for g in groups:
            best_conf = max(
                (d.confidence for d in g),
                key=lambda c: _TC_CONF_RANK.get((c or '').lower(), 0),
            )
            channel_label = g[0].channel
            details = g[0].details
            span_start = max(file_start, min(d.start_time for d in g) - _TC_GAP_REFINE_MARGIN_SEC)
            span_end = min(file_end, max(d.end_time for d in g) + _TC_GAP_REFINE_MARGIN_SEC)
            for rs, re in _tc_find_tc_subregions(frames, channel, span_start, span_end):
                refined.append(_TCDetection(
                    start_time=rs, end_time=re, criterion=crit,
                    channel=channel_label, confidence=best_conf, details=details,
                ))

    refined.sort(key=lambda d: d.start_time)
    return refined


def _detect_astats_channel_tc(frames, times, channel):
    """Detect TC on a single channel using rolling windows of astats data."""
    prefix = f'lavfi.astats.{channel}.'
    detections = []

    if len(times) >= 2:
        dt_sec = times[1] - times[0]
    else:
        dt_sec = 0.1
    if dt_sec <= 0:
        dt_sec = 0.1
    window = max(1, int(round(_TC_ASTATS_WINDOW_SEC / dt_sec)))

    for i in range(0, len(frames) - window + 1, max(1, window // 2)):
        end = min(i + window, len(frames))
        w_frames = frames[i:end]

        rms_levels = []
        crest_factors = []
        zcr_rates = []
        entropies = []

        for f in w_frames:
            tags = f['tags']
            rms = tags.get(prefix + 'RMS_level', float('nan'))
            crest = tags.get(prefix + 'Crest_factor', float('nan'))
            zcr = tags.get(prefix + 'Zero_crossings_rate', float('nan'))
            ent = tags.get(prefix + 'Entropy', float('nan'))

            if not math.isnan(rms):
                rms_levels.append(rms)
            if not math.isnan(crest):
                crest_factors.append(crest)
            if not math.isnan(zcr):
                zcr_rates.append(zcr)
            if not math.isnan(ent):
                entropies.append(ent)

        if not rms_levels:
            continue

        rms_mean = _tc_mean(rms_levels)
        rms_std = _tc_stdev(rms_levels)
        crest_mean = _tc_mean(crest_factors) if crest_factors else float('nan')
        crest_std = _tc_stdev(crest_factors) if crest_factors else float('nan')
        zcr_mean = _tc_mean(zcr_rates) if zcr_rates else float('nan')
        zcr_std = _tc_stdev(zcr_rates) if zcr_rates else float('nan')
        ent_mean = _tc_mean(entropies) if entropies else float('nan')
        ent_std = _tc_stdev(entropies) if entropies else float('nan')

        reasons = []
        passes = True

        if (_TC_ASTATS_RMS_LEVEL_MIN <= rms_mean <= _TC_ASTATS_RMS_LEVEL_MAX
                and rms_std < _TC_ASTATS_RMS_STDEV_MAX):
            reasons.append(f'Signal level: {rms_mean:.1f} dBFS '
                           f'(stability +/-{rms_std:.2f})')
        else:
            passes = False

        if (not math.isnan(crest_mean)
                and _TC_ASTATS_CREST_FACTOR_MIN <= crest_mean <= _TC_ASTATS_CREST_FACTOR_MAX
                and crest_std < _TC_ASTATS_CREST_STDEV_MAX):
            reasons.append(f'Crest factor: {crest_mean:.2f} '
                           f'(stability +/-{crest_std:.2f})')
        else:
            passes = False

        if (not math.isnan(zcr_mean)
                and _TC_ASTATS_ZERO_CROSSINGS_RATE_MIN <= zcr_mean <= _TC_ASTATS_ZERO_CROSSINGS_RATE_MAX
                and zcr_std < _TC_ASTATS_ZCR_STDEV_MAX):
            reasons.append(f'Zero-crossing rate: {zcr_mean:.3f} per frame '
                           f'(stability +/-{zcr_std:.3f})')
        else:
            passes = False

        if (not math.isnan(ent_mean)
                and _TC_ASTATS_ENTROPY_MIN <= ent_mean <= _TC_ASTATS_ENTROPY_MAX
                and ent_std < _TC_ASTATS_ENTROPY_STDEV_MAX):
            reasons.append(f'Spectral entropy: {ent_mean:.2f} '
                           f'(stability +/-{ent_std:.2f})')
        else:
            passes = False

        if passes:
            # Tighter stability across all four metrics → high confidence
            high_conf = (rms_std < 0.5
                         and crest_std < 0.10
                         and zcr_std < 0.005
                         and ent_std < 0.02)
            confidence = 'high' if high_conf else 'medium'
            detections.append(_TCDetection(
                start_time=times[i],
                end_time=times[min(end - 1, len(times) - 1)],
                criterion=f'astats (ch{channel})',
                channel=f'ch{channel}',
                confidence=confidence,
                details=', '.join(reasons)
            ))

    return _tc_filter_by_consecutive(detections, _TC_ASTATS_MIN_WINDOWS)


def _detect_and_write_timecode_results(tc_frames, tc_metric_type, report_directory, fps=None,
                                       tc_start_frames=0, tc_drop_frame=False):
    """Run audible timecode detection and write results to CSV.

    Region start/end positions are written as timecode (HH:MM:SS:FF NDF or
    HH:MM:SS;FF DF) at `fps`, offset by the stream start timecode, so they line
    up with an NLE; durations stay as elapsed M:SS.s.
    """
    def _fmt_tc(seconds):
        return _tc_format_timecode(seconds, fps, tc_start_frames, tc_drop_frame)
    if not tc_frames:
        logger.debug("No audio frame data available for audible timecode detection\n")
        return None

    if tc_metric_type == 'r128':
        detections = _detect_r128_timecode(tc_frames)
    elif tc_metric_type == 'astats':
        detections = _detect_astats_timecode(tc_frames)
    elif tc_metric_type == 'both':
        # Run both detectors and merge — astats is per-channel, R128 is whole-mix.
        # The criterion field on each detection records which one fired.
        detections = _detect_r128_timecode(tc_frames) + _detect_astats_timecode(tc_frames)
        detections.sort(key=lambda d: d.start_time)
    else:
        logger.debug("No recognized audio metrics (r128 or astats) found for timecode detection\n")
        return None

    duration = tc_frames[-1]['time'] if tc_frames else 0

    # Collapse the overlapping per-method detections into consensus regions —
    # this is what the report shows and what "Regions Detected" now counts.
    # When per-channel astats data exists, an R128 detection astats never
    # corroborated is discarded, so "detected" follows the consensus, not the
    # raw per-method list.
    consensus = _tc_build_consensus(
        detections, astats_available=bool(_tc_astats_channels(tc_frames)))
    tc_detected = len(consensus) > 0

    results = {
        'timecode_detected': tc_detected,
        'metric_type': tc_metric_type,
        'total_audio_frames': len(tc_frames),
        'duration': duration,
        'consensus_regions': [
            {
                'start_time': r.start_time,
                'end_time': r.end_time,
                'channel': r.channel,
                'confidence': r.confidence,
                'methods': r.methods,
                'method_count': r.method_count,
            }
            for r in consensus
        ],
        'detections': [
            {
                'start_time': d.start_time,
                'end_time': d.end_time,
                'criterion': d.criterion,
                'channel': d.channel,
                'confidence': d.confidence,
                'details': d.details,
            }
            for d in detections
        ],
    }

    # Write CSV. The consensus regions are the primary table (the report reads
    # these); the raw per-method detections follow in a separate section as a
    # forensic record and are not shown in the report.
    tc_csv = os.path.join(report_directory, "qct-parse_audible_timecode.csv")
    with open(tc_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Audible Timecode Detection Results"])
        writer.writerow(["Metric Type", tc_metric_type])
        writer.writerow(["Total Audio Frames", len(tc_frames)])
        writer.writerow(["Duration", _tc_format_time(duration)])
        writer.writerow(["Audible Timecode Detected", "Yes" if tc_detected else "No"])
        writer.writerow(["Regions Detected", len(consensus)])
        writer.writerow([])

        if consensus:
            writer.writerow(["Start Time", "End Time", "Duration", "Channel", "Confidence", "Detection Methods"])
            for r in consensus:
                writer.writerow([
                    _fmt_tc(r.start_time),
                    _fmt_tc(r.end_time),
                    _tc_format_time(r.end_time - r.start_time),
                    r.channel,
                    r.confidence,
                    r.methods,
                ])
            writer.writerow([])

        if detections:
            writer.writerow(["Per-Method Detections", len(detections)])
            writer.writerow([])
            writer.writerow(["Start Time", "End Time", "Criterion", "Channel", "Confidence", "Details"])
            for d in detections:
                writer.writerow([
                    _fmt_tc(d.start_time),
                    _fmt_tc(d.end_time),
                    d.criterion,
                    d.channel,
                    d.confidence,
                    d.details,
                ])

    # Log summary
    if tc_detected:
        regions = "; ".join(
            f"{_fmt_tc(r.start_time)}-{_fmt_tc(r.end_time)} "
            f"[{r.confidence}] {r.methods}"
            for r in consensus
        )
        logger.warning(
            f"Audible timecode detected: {len(consensus)} consensus region(s) "
            f"from {len(detections)} per-method detection(s) — {regions}\n"
        )
    else:
        logger.debug("No audible timecode detected\n")

    return results



def _get_video_duration(video_path):
    """Video duration in seconds, or None. See utils.ffprobe_probe."""
    return ffprobe_probe.duration(video_path)



def _get_audio_stream_count(video_path):
    """Number of separate audio streams, or None if it can't be determined.

    A count greater than 1 means the multi-stream layout (e.g. broadcast MXF,
    where each audio channel is its own discrete mono stream): the QCTools
    report's audio frames cannot describe the real per-stream audio, so audio
    analysis reads a generated per-stream stats sidecar instead
    (checks/audio_stream_stats.py).
    """
    return ffprobe_probe.audio_stream_count(video_path)


def _detect_audio_pkt(stats_path):
    """Determine the timestamp attribute (pkt_dts_time or pkt_pts_time) used by
    the frames of an audio stats sidecar (checks/audio_stream_stats.py output).

    Mirrors the video-frame detection at the top of run_qctparse(), but scans
    audio frames since the sidecar contains no video frames. Defaults to
    pkt_dts_time if no match is found.
    """
    try:
        with gzip.open(stats_path, 'rt', encoding='utf-8') as f:
            for line in f:
                if '<frame ' in line:
                    match = re.search(r'pkt_.ts_time', line)
                    if match:
                        return match.group()
    except OSError:
        pass
    return 'pkt_dts_time'



def _get_video_frame_rate(video_path):
    """Frames per second (e.g. 29.97 for NTSC), or None.

    Feeds the file-aware NDF/DF timecode conversion; see _tc_format_timecode.
    """
    return ffprobe_probe.frame_rate(video_path)



def _get_video_start_timecode(video_path):
    """The stream's start timecode tag (e.g. '00:00:00:09' NDF, '01:00:00;00'
    DF), or None when the file carries no timecode tag."""
    return ffprobe_probe.start_timecode(video_path)


def _time_in_bars_region(t, bars_regions, margin=DROPOUT_BARS_MARGIN_SEC):
    """True if time `t` (seconds) falls inside a detected color-bars region, or
    within `margin` seconds after one.

    A reference tone shutting off at the end of a bars+tone segment produces the
    same loud-steady -> silence RMS cliff a tape dropout does, so candidates in
    the bars region (and just past its end, where the tone-off transition lands)
    are false positives for dropout. `bars_regions` is the `all_bars_regions`
    list of (label, start_sec, end_sec) tuples from run_qctparse; entries with a
    missing start or end are skipped.
    """
    if not bars_regions:
        return False
    for region in bars_regions:
        start, end = region[1], region[2]
        if start is None or end is None:
            continue
        if start <= t <= end + margin:
            return True
    return False


def _is_zcr_only(event):
    """True if the event's sole corroborating metric is the Zero_crossings_rate
    spike. ZCR climbs during ordinary silence (the noise floor hovers around
    zero) without the Max/RMS_difference collapse a real tape dropout shows, so a
    ZCR-only event is the dominant single-channel false-positive signature."""
    return event.corroborating == ['Zero_crossings_rate spike']


def _zcr_only_has_cross_channel_support(event, all_events,
                                        tolerance=DROPOUT_ZCR_XCHAN_TOLERANCE_SEC):
    """True if a ZCR-only event overlaps in time (within `tolerance` seconds)
    with a detection on a *different* channel that carries a stronger metric
    (Max_difference / RMS_difference drop).

    A real dropout that briefly silences one channel almost always disturbs the
    other, where the RMS-based corroborators fire; an isolated ZCR-only event
    with no such cross-channel support is natural silence, not a dropout."""
    for other in all_events:
        if other is event or other.channel == event.channel:
            continue
        # Require the supporting channel to have a non-ZCR corroborator so two
        # simultaneous silent channels can't prop each other up.
        if not any(c != 'Zero_crossings_rate spike' for c in other.corroborating):
            continue
        if (event.start_time - tolerance) <= other.end_time and \
           (other.start_time - tolerance) <= event.end_time:
            return True
    return False


def _merge_dropout_candidates(candidates):
    """Merge consecutive dropout candidates into events.

    Candidates on the same channel within DROPOUT_MERGE_GAP_SEC of each other
    are merged into a single _DropoutEvent.
    """
    if not candidates:
        return []

    # Sort by channel then time
    sorted_cands = sorted(candidates, key=lambda c: (c.channel, c.time))

    events = []
    current = sorted_cands[0]
    event_start = current.time
    event_end = current.time
    event_channel = current.channel
    worst_rms = current.rms_level
    best_median = current.median_rms
    all_corroborating = set(current.corroborating)
    frames_in_event = 1

    for cand in sorted_cands[1:]:
        if cand.channel == event_channel and (cand.time - event_end) <= DROPOUT_MERGE_GAP_SEC:
            # Merge into current event
            event_end = cand.time
            if cand.rms_level < worst_rms:
                worst_rms = cand.rms_level
            best_median = max(best_median, cand.median_rms)
            all_corroborating.update(cand.corroborating)
            frames_in_event += 1
        else:
            # Finalize current event
            n_corr = len(all_corroborating)
            if n_corr >= 2:
                confidence = 'high'
            elif n_corr == 1:
                confidence = 'medium'
            else:
                confidence = 'low'
            events.append(_DropoutEvent(
                start_time=event_start,
                end_time=event_end,
                channel=event_channel,
                worst_rms_level=worst_rms,
                median_rms_level=best_median,
                confidence=confidence,
                corroborating=sorted(all_corroborating),
            ))
            # Start new event
            event_start = cand.time
            event_end = cand.time
            event_channel = cand.channel
            worst_rms = cand.rms_level
            best_median = cand.median_rms
            all_corroborating = set(cand.corroborating)
            frames_in_event = 1

    # Finalize last event
    n_corr = len(all_corroborating)
    if n_corr >= 2:
        confidence = 'high'
    elif n_corr == 1:
        confidence = 'medium'
    else:
        confidence = 'low'
    events.append(_DropoutEvent(
        start_time=event_start,
        end_time=event_end,
        channel=event_channel,
        worst_rms_level=worst_rms,
        median_rms_level=best_median,
        confidence=confidence,
        corroborating=sorted(all_corroborating),
    ))

    return events


def _detect_and_write_dropout_results(dropout_candidates, report_directory, total_audio_frames,
                                      fps=None, tc_start_frames=0, tc_drop_frame=False,
                                      bars_regions=None):
    """Merge dropout candidates into events, write CSV, and return results dict.

    Event Timestamp Start/End are written as the file's own timecode (NDF
    HH:MM:SS:FF or DF HH:MM:SS;FF) at `fps`, offset by the stream start
    timecode, so they line up with an NLE.

    `bars_regions` (the run_qctparse `all_bars_regions` list) suppresses
    candidates inside a color-bars region or just past its end — a reference
    tone shutting off there mimics a dropout's RMS cliff but isn't one.
    """

    # Drop candidates that fall in a color-bars/tone region before merging.
    if bars_regions:
        kept = [c for c in dropout_candidates if not _time_in_bars_region(c.time, bars_regions)]
        suppressed = len(dropout_candidates) - len(kept)
        if suppressed:
            logger.debug(f"Dropout: suppressed {suppressed} candidate(s) in color-bars regions\n")
        dropout_candidates = kept

    events = _merge_dropout_candidates(dropout_candidates)

    # Filter out long events without strong corroboration.
    # Real tape dropout is very brief; multi-second events with no
    # corroborating metrics are almost certainly false positives.
    events = [
        ev for ev in events
        if (ev.end_time - ev.start_time) <= DROPOUT_LONG_EVENT_SEC
        or len(ev.corroborating) >= DROPOUT_LONG_EVENT_MIN_CORR
    ]

    # Suppress ZCR-only events (natural-silence false positives) unless another
    # channel corroborates them at the same time. Filtered against the full event
    # list so a kept event still counts as support for its cross-channel twin.
    zcr_kept = [
        ev for ev in events
        if not _is_zcr_only(ev) or _zcr_only_has_cross_channel_support(ev, events)
    ]
    zcr_suppressed = len(events) - len(zcr_kept)
    if zcr_suppressed:
        logger.debug(
            f"Dropout: suppressed {zcr_suppressed} ZCR-only event(s) "
            f"without cross-channel support\n")
    events = zcr_kept

    dropout_detected = len(events) > 0
    frames_flagged = len(dropout_candidates)

    results = {
        'dropout_detected': dropout_detected,
        'dropout_events': len(events),
        'frames_flagged': frames_flagged,
        'total_audio_frames': total_audio_frames,
        'events': events,
    }

    audio_dropout_csv = os.path.join(report_directory, "qct-parse_audio_dropout.csv")
    with open(audio_dropout_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Audio Dropout Detection Results"])
        writer.writerow(["Rolling Window Size (frames)", DROPOUT_ROLLING_WINDOW_SIZE])
        writer.writerow(["RMS Drop Threshold (dB)", DROPOUT_RMS_DROP_THRESHOLD_DB])
        writer.writerow(["Silence Floor (dBFS)", DROPOUT_SILENCE_FLOOR_DB])
        writer.writerow(["Total Audio Frames", total_audio_frames])
        writer.writerow(["Dropout Events Detected", len(events)])
        writer.writerow(["Frames Flagged", frames_flagged])
        writer.writerow(["Dropout Detected", "Yes" if dropout_detected else "No"])
        writer.writerow([])

        if events:
            writer.writerow(["Timestamp Start", "Timestamp End", "Channel",
                             "Worst RMS (dBFS)", "Median RMS (dBFS)", "Drop (dB)",
                             "Confidence", "Corroborating Metrics"])
            for ev in events:
                drop_db = ev.median_rms_level - ev.worst_rms_level
                writer.writerow([
                    _tc_format_timecode(ev.start_time, fps, tc_start_frames, tc_drop_frame),
                    _tc_format_timecode(ev.end_time, fps, tc_start_frames, tc_drop_frame),
                    ev.channel,
                    f"{ev.worst_rms_level:.1f}",
                    f"{ev.median_rms_level:.1f}",
                    f"{drop_db:.1f}",
                    ev.confidence,
                    ", ".join(ev.corroborating) if ev.corroborating else "None",
                ])

    if dropout_detected:
        high_count = sum(1 for e in events if e.confidence == 'high')
        med_count = sum(1 for e in events if e.confidence == 'medium')
        low_count = sum(1 for e in events if e.confidence == 'low')
        logger.warning(
            f"Audio dropout detected: {len(events)} event(s) "
            f"({high_count} high, {med_count} medium, {low_count} low confidence), "
            f"{frames_flagged} frame(s) flagged\n"
        )
    else:
        logger.debug("No audio dropout detected\n")

    return results


def run_qctparse(video_path, qctools_output_path, report_directory, check_cancelled=None, signals=None, clams_regions=None):
    """
    Executes the qct-parse analysis on a given video file, exporting relevant data and thumbnails based on specified thresholds and profiles.

    Parameters:
        video_path (str): Path to the video file being analyzed.
        qctools_output_path (str): Path to the QCTools XML report output.
        report_directory (str): Path to {video_id}_report_csvs directory.
        check_cancelled (callable): Optional function to check if processing was cancelled.
        signals: Optional signals object for emitting progress updates.
        clams_regions (list or None): List of (start_seconds, end_seconds, label)
            tuples from CLAMS detection. qct-parse runs additional detectBars()
            passes on regions beyond the head scan window.

    Returns:
        dict: {'head_bars_start', 'head_bars_end', 'all_bars_regions'} or None if cancelled.
    """
    
    checks_config = config_mgr.get_config('checks', ChecksConfig)
    spex_config = config_mgr.get_config('spex', SpexConfig)

    fullTagList = getFullTagList()

    # Check if we can load required library
    etree = load_etree()
    if etree is None:
        logger.critical("Cannot proceed with qct-parse: required library lxml.etree is not available")
        return None
    
    logger.info("Starting qct-parse\n")

    # Estimate total video frames for progress reporting
    total_duration = _get_video_duration(video_path)
    # Frame rate and the stream's start timecode are used to report
    # audible-timecode positions as the file's own timecode, matching the NLE.
    video_fps = _get_video_frame_rate(video_path)
    _tc_nominal = max(1, int(round(video_fps))) if video_fps else 30
    tc_start_frames, tc_drop_frame = _tc_parse_start_timecode(
        _get_video_start_timecode(video_path), _tc_nominal
    )

    ###### Initialize variables ######
    startObj = qctools_output_path
    
    qct_parse = asdict(checks_config.tools.qct_parse)

    # Get the extension from the actual qctools file path
    if '.qctools.mkv' in qctools_output_path:
        qctools_ext = 'qctools.mkv'
    elif '.qctools.xml.gz' in qctools_output_path:
        qctools_ext = 'qctools.xml.gz'
    else:
        # Fallback to config if we can't determine from path
        qctools_ext = checks_config.outputs.qctools_ext

    if qctools_ext.lower().endswith('mkv'):
        startObj = extract_report_mkv(startObj, qctools_output_path)
        if startObj is None:
            # Extraction failed (reason logged by extract_report_mkv). Skip
            # qct-parse rather than taking down the rest of output processing.
            logger.critical(
                "Skipping qct-parse: could not read the QCTools report "
                f"{os.path.basename(qctools_output_path)}\n"
            )
            return None

    # Initalize circular buffer for efficient xml parsing
    buffSize = int(11)
    framesList = collections.deque(maxlen=buffSize) # init framesList

    # Set parentDir and baseName
    parentDir = os.path.dirname(startObj)
    baseName = (os.path.basename(startObj)).split('.')[0]

    # Initialize thumbExport delay, will be updated per use case
    thumbDelay = 9000
    thumbExportDelay = thumbDelay

    # initialize the start and end duration times variables
    durationStart = 0
    durationEnd = 99999999
    # End of detected color bars region (in seconds). Captured below if bars
    # detection runs; consumed by downstream features that need to skip the
    # bars at the head of the tape (e.g. chroma-phase detection).
    chroma_bars_end_time = None

    # set the path for the thumbnail export
    thumbPath = os.path.join(report_directory, "ThumbExports")
    if qct_parse['thumbExport']:
        if not os.path.exists(thumbPath):
            os.makedirs(thumbPath)
        else:
            archive_result = archiveThumbs(thumbPath)
            if archive_result:
                logger.debug(f"Archived thumbnails to {archive_result}\n")

    profile = {}  # init a dictionary where we'll store reference values from config.yaml file

    # init a list of every tag available in a QCTools Report from the fullTagList in the config.yaml
    tagList = list(fullTagList.keys())

    # open qctools report 
    # determine if report stores pkt_dts_time or pkt_pts_time
    with gzip.open(startObj) as xml:    
        for event, elem in etree.iterparse(xml, events=('end',), tag='frame'):  # iterparse the xml doc
            if elem.attrib['media_type'] == "video":  # get just the video frames
                # we gotta find out if the qctools report has pkt_dts_time or pkt_pts_time ugh
                match = re.search(r"pkt_.ts_time", etree.tostring(elem).decode('utf-8'))
                if match:
                    pkt = match.group()
                    break

    # Determine if video values are 10 bit depth
    bit_depth_10 = detectBitdepth(startObj,pkt,framesList,buffSize)

    if signals and hasattr(signals, 'qctparse_progress'):
        signals.qctparse_progress.emit(0)

    if check_cancelled():
        return None

    ######## Iterate Through the XML for Bars detection ########
    all_bars_regions = []

    if qct_parse['barsDetection']:
        durationStart = ""                            # if bar detection is turned on then we have to calculate this
        durationEnd = ""                            # if bar detection is turned on then we have to calculate this
        logger.debug(f"Starting Bars Detection on {baseName}\n")
        qctools_colorbars_duration_output = os.path.join(report_directory, "qct-parse_colorbars_durations.csv")
        durationStart, durationEnd, barsStartString, barsEndString = detectBars(startObj,pkt,durationStart,durationEnd,framesList,buffSize,bit_depth_10,signals=signals,total_duration=total_duration)

        # Handle case where bars start was found but no end transition was detected
        # This happens when the entire video is color bars
        if barsStartString and not barsEndString:
            logger.debug("Color bars start detected but no end found - checking if entire video is color bars\n")
            # Reset framesList for validation
            framesList.clear()
            is_entire_video_bars, video_end_time = validateEntireVideoAsBars(startObj, pkt, durationStart, framesList, buffSize, bit_depth_10)

            if is_entire_video_bars and video_end_time is not None:
                durationEnd = video_end_time
                barsEndString = dts2ts(str(video_end_time))
                logger.info(f"Entire video confirmed as color bars - setting end duration to {barsEndString}\n")
            else:
                logger.warning("Could not confirm entire video as color bars\n")

        if barsStartString and barsEndString:
            head_start_sec = float(durationStart) if durationStart not in ("", None) else None
            head_end_sec = float(durationEnd) if durationEnd not in ("", None) else None
            all_bars_regions.append(("head", head_start_sec, head_end_sec))
            if qct_parse['thumbExport']:
                barsStampString = dts2ts(durationStart)
                printThumb(video_path, "bars_found", "color_bars_detection", startObj,thumbPath, "first_frame", barsStampString)
        elif durationStart == "" and durationEnd == "":
            logger.error("No color bars detected\n")

        HEAD_SCAN_WINDOW = 300.0

        # If qct-parse missed head bars but CLAMS found them, retry with
        # relaxed thresholds on the CLAMS-identified window.
        head_bars_found = any(r[0] == "head" for r in all_bars_regions)
        if not head_bars_found and clams_regions:
            head_clams_bars = [
                (s, e) for s, e, label in clams_regions
                if s < HEAD_SCAN_WINDOW and label == "bars"
            ]
            if head_clams_bars:
                # Cluster from the earliest run: extend only through runs that
                # start within 30s of the cluster end, so bars found much later
                # in the scan window (e.g. tail bars) don't stretch the head
                # retry window and defeat the end clamp below.
                clustered = sorted(head_clams_bars, key=lambda t: t[0])
                clams_start, clams_end = clustered[0]
                for s, e in clustered[1:]:
                    if s - clams_end <= 30.0:
                        clams_end = max(clams_end, e)
                    else:
                        break
                logger.info(
                    f"qct-parse head bars retry: CLAMS found bars at "
                    f"{clams_start:.1f}s–{clams_end:.1f}s but qct-parse "
                    f"did not — re-running with relaxed thresholds"
                )
                framesList.clear()
                r_start, r_end, r_start_str, r_end_str = detectBars(
                    startObj, pkt, "", "", framesList, buffSize, bit_depth_10,
                    search_start_time=max(0.0, clams_start - 5.0),
                    search_end_time=clams_end + 5.0,
                    relaxed=True,
                )
                if r_start_str and r_end_str:
                    r_start_sec = float(r_start) if r_start not in ("", None) else None
                    r_end_sec = float(r_end) if r_end not in ("", None) else None
                    # This is a confirmation pass, not discovery: CLAMS seeded the
                    # window, and relaxed luma thresholds can't tell bars from a
                    # saturated slate, so a retry that runs past the CLAMS end is
                    # overshoot (typically riding into the search-window edge).
                    # Trust the SSIM-derived CLAMS boundary instead.
                    if r_end_sec is not None and r_end_sec > clams_end + 1.0:
                        logger.info(
                            f"qct-parse relaxed retry ran past CLAMS bars end "
                            f"({r_end_sec:.1f}s > {clams_end:.1f}s) — clamping to CLAMS end"
                        )
                        r_end_sec = clams_end
                        r_end = clams_end
                        r_end_str = dts2ts(str(clams_end))
                    if r_start_sec is not None and r_start_sec < clams_start - 1.0:
                        r_start_sec = clams_start
                        r_start = clams_start
                        r_start_str = dts2ts(str(clams_start))
                    all_bars_regions.append(("head-relaxed", r_start_sec, r_end_sec))
                    durationStart = r_start
                    durationEnd = r_end
                    barsStartString = r_start_str
                    barsEndString = r_end_str
                    chroma_bars_end_time = r_end_sec
                    logger.info(f"qct-parse relaxed retry found bars: {r_start_str} – {r_end_str}")
                    if qct_parse['thumbExport']:
                        barsStampString = dts2ts(r_start)
                        printThumb(video_path, "bars_found", "color_bars_detection", startObj, thumbPath, "first_frame", barsStampString)
                else:
                    logger.info("qct-parse relaxed retry: still no bars confirmed")

        # CLAMS-guided windowed scans. Merge overlapping CLAMS regions
        # first so that bars + tone detections covering the same mid-file
        # bars produce one scan, not two. Skip regions that overlap with
        # the already-detected head bars.
        head_region_found = next(
            (r for r in all_bars_regions if r[0] in ("head", "head-relaxed")), None
        )
        if clams_regions:
            filtered = []
            for region_start, region_end, region_label in clams_regions:
                if head_region_found:
                    _, head_s, head_e = head_region_found
                    if (head_s is not None and head_e is not None and
                            region_start <= head_e + 5.0 and region_end >= head_s - 5.0):
                        # Overlaps the detected head bars: this is the head
                        # sequence itself (tone routinely outlasts the bars),
                        # and rescanning it just re-finds the head bars as an
                        # "additional" region. Only scan a remainder that
                        # extends well past the head region.
                        remainder_start = head_e + 5.0
                        if region_end - remainder_start >= 30.0:
                            filtered.append((remainder_start, region_end))
                        continue
                filtered.append((region_start, region_end))

            # Merge overlapping/adjacent filtered regions
            merged_windows = []
            if filtered:
                sorted_f = sorted(filtered, key=lambda t: t[0])
                merged_windows = [sorted_f[0]]
                for s, e in sorted_f[1:]:
                    prev_s, prev_e = merged_windows[-1]
                    if s <= prev_e + 5.0:
                        merged_windows[-1] = (prev_s, max(prev_e, e))
                    else:
                        merged_windows.append((s, e))

            for window_idx, (window_start, window_end) in enumerate(merged_windows):
                if check_cancelled and check_cancelled():
                    break
                region_label = f"additional-{window_idx + 1}"
                logger.info(
                    f"qct-parse windowed bars scan: {window_start:.1f}s–{window_end:.1f}s "
                    f"({region_label})"
                )
                framesList.clear()
                w_start, w_end, w_start_str, w_end_str = detectBars(
                    startObj, pkt, "", "", framesList, buffSize, bit_depth_10,
                    search_start_time=window_start,
                    search_end_time=window_end,
                )
                if not (w_start_str and w_end_str):
                    logger.info(
                        f"qct-parse windowed scan: no bars at normal thresholds in "
                        f"{window_start:.1f}s–{window_end:.1f}s — retrying relaxed"
                    )
                    framesList.clear()
                    w_start, w_end, w_start_str, w_end_str = detectBars(
                        startObj, pkt, "", "", framesList, buffSize, bit_depth_10,
                        search_start_time=window_start,
                        search_end_time=window_end,
                        relaxed=True,
                    )
                if w_start_str and w_end_str:
                    w_start_sec = float(w_start) if w_start not in ("", None) else None
                    w_end_sec = float(w_end) if w_end not in ("", None) else None
                    all_bars_regions.append((region_label, w_start_sec, w_end_sec))
                    logger.info(f"qct-parse windowed scan found bars: {w_start_str} – {w_end_str}")
                    if qct_parse['thumbExport']:
                        thumbStamp = dts2ts(str(w_start))
                        printThumb(video_path, "bars_found", f"additional_bars_{window_idx + 1}", startObj, thumbPath, "first_frame", thumbStamp)
                else:
                    logger.debug(
                        f"qct-parse windowed scan: no bars confirmed in "
                        f"{window_start:.1f}s–{window_end:.1f}s (even with relaxed thresholds)"
                    )

        # Write CSV with all detected regions
        if all_bars_regions:
            csv_regions = [
                (label, dts2ts(str(start)), dts2ts(str(end)))
                for label, start, end in all_bars_regions
                if start is not None and end is not None
            ]
            print_bars_durations(qctools_colorbars_duration_output, regions=csv_regions)
        else:
            print_bars_durations(qctools_colorbars_duration_output, regions=[])

        # Capture head bars end (seconds) before durationEnd may be clobbered below,
        # so downstream features can skip the colour-bars region.
        try:
            if durationEnd not in ("", None):
                chroma_bars_end_time = float(durationEnd)
        except (TypeError, ValueError):
            chroma_bars_end_time = None

    if check_cancelled():
        return None

    if signals and hasattr(signals, 'qctparse_progress'):
        signals.qctparse_progress.emit(5)

    ######## Iterate Through the XML for Bars Evaluation ########
    if qct_parse['evaluateBars']:
        bars_fallback = False
        smpte_selected = False
        # Detected bar values measured for the report graph in "smpte" mode
        # (the evaluation itself still grades against SMPTE); None when no bars
        # were detected.
        smpte_detected_values = None
        # "detected" (default): grade content against this file's own detected
        # bars, falling back to SMPTE values when none are found. "smpte":
        # always grade against standard SMPTE values, regardless of detection.
        evaluate_reference = qct_parse.get('evaluateBarsReference', 'detected')

        if evaluate_reference == 'smpte':
            logger.info("Evaluate Color Bars: grading against standard SMPTE color bars values (user-selected reference).\n")
            maxBarsDict = asdict(spex_config.qct_parse_values.smpte_color_bars)
            smpte_selected = True
            # If bars were detected, measure them too so the report can still
            # render the informational detected-vs-SMPTE graph. These values
            # are graph-only; the evaluation grades against maxBarsDict (SMPTE).
            if qct_parse['barsDetection'] and durationStart != "" and durationEnd != "":
                smpte_detected_values = evalBars(startObj, pkt, durationStart, durationEnd, framesList, buffSize)
        elif qct_parse['barsDetection'] and durationStart == "" and durationEnd == "":
            logger.warning(f"No color bars found - falling back to SMPTE color bars values from config.\n")
            maxBarsDict = asdict(spex_config.qct_parse_values.smpte_color_bars)
            bars_fallback = True
        elif qct_parse['barsDetection'] and durationStart != "" and durationEnd != "":
            maxBarsDict = evalBars(startObj,pkt,durationStart,durationEnd,framesList,buffSize)
            if maxBarsDict is None:
                logger.critical("Something went wrong - Cannot run evaluate color bars\n")
        else:
            logger.critical("Cannot run color bars evaluation without running Bars Detection.")

        if maxBarsDict is not None:
            logger.debug(f"Starting qct-parse color bars evaluation on {baseName}\n")
            smpte_color_bars = asdict(spex_config.qct_parse_values.smpte_color_bars)
            colorbars_values_output = os.path.join(report_directory, "qct-parse_colorbars_values.csv")

            if bars_fallback:
                with open(colorbars_values_output, 'w') as f:
                    f.write("SMPTE_FALLBACK\n")
            elif smpte_selected:
                if smpte_detected_values is not None:
                    # Bars were detected: write the real detected-vs-SMPTE
                    # comparison CSV so the report renders the graph. The
                    # report reads evaluateBarsReference to also show the
                    # "SMPTE selected" info box above it.
                    print_color_bar_values(baseName, smpte_color_bars, smpte_detected_values, colorbars_values_output)
                else:
                    with open(colorbars_values_output, 'w') as f:
                        f.write("SMPTE_SELECTED\n")
            else:
                print_color_bar_values(baseName, smpte_color_bars, maxBarsDict, colorbars_values_output)
            
            durationStart = 0
            durationEnd = 99999999
            profile = maxBarsDict
            profile_name = 'color_bars_evaluation'
            thumbExportDelay = 9000
            # Skip the detected bars regions themselves (head + additional):
            # the evaluation scores content against the bars-derived
            # thresholds, and scoring bars against bars is self-referential
            # (additional bars would just flag against the head-bars values).
            bars_skip_regions = [
                (region_start, region_end)
                for _, region_start, region_end in all_bars_regions
                if region_start is not None and region_end is not None
            ]
            kbeyond, frameCount, overallFrameFail, failureInfo = analyzeIt(qct_parse, video_path, profile, profile_name, startObj, pkt, durationStart, durationEnd, thumbPath, thumbDelay, thumbExportDelay, framesList, frameCount=0, overallFrameFail=0, adhoc_tag=False, check_cancelled=check_cancelled, signals=signals, total_duration=total_duration, skip_regions=bars_skip_regions)
            colorbars_eval_fails_csv_path = os.path.join(report_directory, "qct-parse_colorbars_eval_failures.csv")
            if failureInfo:
                save_failures_to_csv(failureInfo, colorbars_eval_fails_csv_path)
            qctools_bars_eval_check_output = os.path.join(report_directory, "qct-parse_colorbars_eval_summary.csv")
            printresults(profile, kbeyond, frameCount, overallFrameFail, qctools_bars_eval_check_output)
            logger.debug(f"qct-parse bars evaluation complete. qct-parse summary written to {qctools_bars_eval_check_output}\n")

        # Evaluate windowed (non-head) bars regions: produce a SMPTE
        # comparison CSV and first-frame thumbnail for each.
        windowed_regions = [r for r in all_bars_regions if r[0] != "head" and r[0] != "head-relaxed"]
        for idx, (region_label, region_start, region_end) in enumerate(windowed_regions):
            if check_cancelled and check_cancelled():
                break
            if region_start is None or region_end is None:
                continue
            logger.info(
                f"Evaluating windowed bars region {idx + 1}: "
                f"{region_label} ({region_start:.1f}s–{region_end:.1f}s)"
            )
            framesList.clear()
            windowed_maxBarsDict = evalBars(startObj, pkt, region_start, region_end, framesList, buffSize)
            if windowed_maxBarsDict is not None:
                smpte_color_bars = asdict(spex_config.qct_parse_values.smpte_color_bars)
                windowed_values_csv = os.path.join(
                    report_directory,
                    f"qct-parse_colorbars_values_{region_label}.csv",
                )
                print_color_bar_values(baseName, smpte_color_bars, windowed_maxBarsDict, windowed_values_csv)
                logger.info(f"Windowed bars values written to {os.path.basename(windowed_values_csv)}")
            else:
                logger.warning(f"Could not evaluate bars for region {region_label}")

    if check_cancelled():
        return None

    if signals and hasattr(signals, 'qctparse_progress'):
        signals.qctparse_progress.emit(36)

    ######## Clamped Levels Detection ########
    if qct_parse.get('detect_clamped_levels', False):
        logger.debug(f"Starting clamped-levels detection on {baseName}\n")
        clamped_results = analyzeClampedLevels(
            startObj, pkt, report_directory, bit_depth_10,
            signals=signals, total_duration=total_duration
        )
        if clamped_results is None:
            logger.warning("Clamped-levels detection could not be performed\n")

    if check_cancelled():
        return None

    ######## Chroma Phase Error Detection ########
    if qct_parse.get('detect_chroma_phase_errors', False):
        logger.debug(f"Starting chroma-phase error detection on {baseName}\n")
        chroma_results = analyzeChromaPhaseErrors(
            startObj, pkt, report_directory, bit_depth_10,
            bars_end_time=chroma_bars_end_time,
            video_path=video_path,
            signals=signals, total_duration=total_duration,
        )
        if chroma_results is None:
            logger.warning("Chroma-phase detection could not be performed\n")

    if check_cancelled():
        return None

    if signals and hasattr(signals, 'qctparse_progress'):
        signals.qctparse_progress.emit(64)

    ######## Audio Analysis (Clipping / Channel Imbalance / Audible Timecode / Dropout / Identical Channels) ########
    do_audio_analysis = qct_parse.get('audio_analysis', False)
    imbalance_results = None
    timecode_results = None
    identical_results = None

    # Multi-stream inputs (e.g. broadcast MXF, where each audio channel is a
    # discrete mono stream): the QCTools report's audio frames describe a qcli
    # downmix of all streams, not the real per-stream audio. Instead of reading
    # the report, generate (or reuse) an audio stats sidecar that decodes the
    # streams and merges them into one N-channel analysis signal — astats
    # channel N is audio stream N, the same 1-based channel <-> stream mapping
    # make_access.py's multi-mono handling assumes — and run the audio checks
    # on that file. Single-stream inputs still read the QCTools report.
    audio_stats_obj = None
    audio_pkt = pkt
    audio_stream_indexes = None
    if do_audio_analysis:
        audio_stream_count = _get_audio_stream_count(video_path)
        if audio_stream_count and audio_stream_count > 1:
            logger.info(
                f"Input has {audio_stream_count} separate audio streams (discrete mono tracks, "
                "typical of broadcast MXF); the QCTools report only carries a downmix of them. "
                "Audio analysis will run on per-stream stats generated from the file instead.\n"
            )
            audio_stats_obj = audio_stats_path(parentDir, baseName)
            if os.path.isfile(audio_stats_obj):
                logger.info(
                    f"Found existing audio stats sidecar {os.path.basename(audio_stats_obj)} — "
                    "reusing it (delete the file to force regeneration)\n"
                )
            else:
                audio_stats_obj = generate_audio_stats_sidecar(
                    video_path, parentDir, baseName, check_cancelled=check_cancelled
                )
            if check_cancelled():
                return None
            if audio_stats_obj is None:
                logger.warning(
                    "Per-stream audio stats could not be generated; audio analysis skipped. "
                    "(Falling back to the QCTools report would describe the downmix, not the "
                    "real streams.)\n"
                )
                do_audio_analysis = False
            else:
                audio_pkt = _detect_audio_pkt(audio_stats_obj)
                # The sidecar merged the audio streams in stream order, so
                # analyzed channel N is the Nth audio stream (0:a:N-1).
                audio_stream_indexes = list(range(audio_stream_count))

    if do_audio_analysis:
        logger.debug(f"Starting audio analysis on {baseName}\n")
        clipping_results, imbalance_results, timecode_results, dropout_results, identical_results = analyzeAudio(
            audio_stats_obj or startObj, audio_pkt, report_directory,
            detect_clipping=True,
            detect_imbalance=True,
            detect_timecode=True,
            detect_dropout=True,
            detect_identical=True,
            signals=signals, total_duration=total_duration, fps=video_fps,
            tc_start_frames=tc_start_frames, tc_drop_frame=tc_drop_frame,
            bars_regions=all_bars_regions,
            video_path=video_path, audio_stream_indexes=audio_stream_indexes,
            check_cancelled=check_cancelled
        )
        if clipping_results is None:
            logger.warning("Audio clipping detection could not be performed\n")
        if imbalance_results is None:
            logger.warning("Channel imbalance analysis could not be performed\n")
        if timecode_results is None:
            logger.warning("Audible timecode detection could not be performed\n")
        if dropout_results is None:
            logger.warning("Audio dropout detection could not be performed\n")
        if identical_results is None:
            logger.debug("Identical-channel detection not applicable\n")

    if check_cancelled():
        return None

    ######## Reference-Tone Leak Detection ########
    # Reads decoded PCM from the video file itself (not the QCTools sidecar),
    # so it supports multi-stream sources and is not gated like audio_analysis.
    if qct_parse.get('detect_tone_leak', False):
        logger.debug(f"Starting tone-leak detection on {baseName}\n")
        tone_leak_results = analyzeToneLeak(
            video_path, report_directory,
            fps=video_fps, tc_start_frames=tc_start_frames,
            tc_drop_frame=tc_drop_frame,
            check_cancelled=check_cancelled, signals=signals,
            total_duration=total_duration,
        )
        if tone_leak_results is None and not check_cancelled():
            logger.warning("Tone-leak detection could not be performed\n")

    if check_cancelled():
        return None

    logger.info(f"qct-parse finished processing file: {os.path.basename(startObj)} \n")

    if signals and hasattr(signals, 'qctparse_progress'):
        signals.qctparse_progress.emit(100)

    head_region = next((r for r in all_bars_regions if r[0] in ("head", "head-relaxed")), None)

    # Audio findings consumed downstream by access file creation (channel
    # exclusion). None when audio analysis is off or produced no results, so
    # the caller can distinguish "nothing flagged" from "analysis didn't run".
    audio_findings = None
    if do_audio_analysis and (imbalance_results or timecode_results):
        audio_findings = {
            'silent_channels': (imbalance_results or {}).get('silent_channels', []),
            'num_channels': (imbalance_results or {}).get('num_channels'),
            'timecode_consensus_regions': (timecode_results or {}).get('consensus_regions', []),
            'duration': total_duration or (timecode_results or {}).get('duration'),
        }

    return {
        'head_bars_start': head_region[1] if head_region else None,
        'head_bars_end': head_region[2] if head_region else None,
        'all_bars_regions': all_bars_regions,
        'audio_findings': audio_findings,
    }


if __name__ == "__main__":
    # if len(sys.argv) != 2:
    #    print("Usage: python qct-parse.py <input_video> <qctools_report>")
    #    sys.exit(1)
    video_path = sys.argv[1]
    report_path = sys.argv[2]
    qctools_check_output = os.path.dirname(video_path)
    if not os.path.isfile(report_path):
        print(f"Error: {report_path} is not a valid file.")
        sys.exit(1)
    run_qctparse(video_path, report_path, qctools_check_output)