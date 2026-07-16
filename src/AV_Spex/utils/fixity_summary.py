import os
import json

from AV_Spex.utils.log_setup import logger


def get_fixity_summary_path(source_directory, video_id):
    """Return the path to the fixity summary JSON in the qc_metadata dir."""
    return os.path.join(
        source_directory,
        f'{video_id}_qc_metadata',
        f'{video_id}_fixity_summary.json'
    )


def update_fixity_summary(source_directory, video_id, section, data):
    """
    Record the outcome of a fixity step in {video_id}_fixity_summary.json.

    The summary lives in {video_id}_qc_metadata/ and is consumed by the HTML
    report. Each fixity step writes its own section ('checksum_output',
    'whole_file', 'stream_fixity') via read-modify-write, so the steps can run
    in any combination or order. A failure to write the summary must never
    fail the fixity step itself, so all errors are caught and logged.

    Args:
        source_directory: Directory containing the video file
        video_id: Video identifier (filename without extension)
        section: Summary section name to set
        data: dict of results for that section
    """
    summary_path = get_fixity_summary_path(source_directory, video_id)
    try:
        os.makedirs(os.path.dirname(summary_path), exist_ok=True)
        summary = {}
        if os.path.isfile(summary_path):
            try:
                with open(summary_path, 'r', encoding='utf-8') as f:
                    summary = json.load(f)
                if not isinstance(summary, dict):
                    summary = {}
            except (json.JSONDecodeError, OSError):
                logger.warning(f'Could not read existing fixity summary {summary_path}; rewriting it.\n')
                summary = {}
        summary['video_id'] = video_id
        summary[section] = data
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2)
    except Exception as e:
        logger.warning(f'Unable to update fixity summary {summary_path}: {e}\n')
