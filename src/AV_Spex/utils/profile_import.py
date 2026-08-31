"""
Shared machinery for importing expected-value profiles from tool output.

MediaInfo and FFprobe both import a JSON sidecar into a sectioned profile
dataclass, compare a file against a saved profile, and report per-section
matches. Only three things actually differ between them: how the raw JSON is
parsed into sections, which fields each section extracts, and the dataclasses
those sections become. Those differences are declared as an ImportSpec; the
operations built on top of one are shared.

The per-tool modules (mediainfo_import, ffprobe_import) keep their own parsing
and field-extraction functions and their own public entry points, which
delegate here.

ExifTool is intentionally not modelled with an ImportSpec: its profile is flat
rather than sectioned, so exiftool_import.py stands on its own.
"""

import dataclasses
import typing
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from AV_Spex.utils.log_setup import logger


@dataclass(frozen=True)
class ImportSection:
    """One section of a sectioned profile.

    key:          the section's name on the profile dataclass ('general')
    source_key:   the section's key in the parsed JSON ('General')
    extract:      raw section dict -> dict of profile-relevant fields
    values_class: the dataclass the extracted fields are built into
    """
    key: str
    source_key: str
    extract: Callable[[Dict[str, Any]], Dict[str, Any]]
    values_class: type


@dataclass(frozen=True)
class ImportSpec:
    """Everything that varies between the sectioned import domains.

    label:         name used in log messages ('MediaInfo')
    profile_class: the profile dataclass assembled from the sections
    sections:      the profile's sections, in declaration order
    parse:         file path -> {source_key: raw section dict}, or None
    skip_fields:   field names excluded from comparison. FFprobe skips 'tags',
                   which is owned by the signal-flow system rather than by
                   FFprobe profiles — the same exclusion config_edit applies in
                   _ffprobe_matches.
    finalize:      optional (section_key, fields) -> fields hook, applied after
                   defaults, for a domain-specific default that introspection
                   cannot supply.
    """
    label: str
    profile_class: type
    sections: Tuple[ImportSection, ...]
    parse: Callable[[str], Optional[Dict[str, Dict[str, Any]]]]
    skip_fields: Tuple[str, ...] = ()
    finalize: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None


def get_dataclass_field_names(dataclass_type) -> List[str]:
    """Get field names from a dataclass type."""
    return [f.name for f in dataclasses.fields(dataclass_type)]


def apply_defaults(fields: Dict[str, Any], dataclass_type) -> Dict[str, Any]:
    """Fill in default values for any fields missing from extracted data.

    Empty list for List fields, empty dict for Dict fields, empty string
    otherwise. Mutates and returns the fields dict.
    """
    type_hints = typing.get_type_hints(dataclass_type)

    for field_info in dataclasses.fields(dataclass_type):
        field_name = field_info.name
        if field_name not in fields:
            field_type = type_hints.get(field_name)
            origin = typing.get_origin(field_type)

            if origin is list or origin is typing.List:
                fields[field_name] = []
            elif origin is dict or origin is typing.Dict:
                fields[field_name] = {}
            else:
                fields[field_name] = ""

    return fields


def extract_sections(spec: ImportSpec, section_data: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Run each section's extractor over the parsed JSON."""
    return {
        section.key: section.extract(section_data.get(section.source_key, {}))
        for section in spec.sections
    }


def import_file_to_profile(spec: ImportSpec, file_path: str):
    """Parse a tool's JSON output and build the domain's profile dataclass.

    Returns the profile, or None if the file could not be parsed or held no
    relevant fields.
    """
    section_data = spec.parse(file_path)
    if not section_data:
        return None

    extracted = extract_sections(spec, section_data)

    if not any(extracted.values()):
        logger.error(f"No relevant fields found in {spec.label} data")
        return None

    built = {}
    for section in spec.sections:
        fields = apply_defaults(extracted[section.key], section.values_class)
        if spec.finalize:
            fields = spec.finalize(section.key, fields)
        extracted[section.key] = fields
        built[section.key] = section.values_class(**fields)

    try:
        profile = spec.profile_class(**built)
    except Exception as e:
        logger.error(f"Failed to create {spec.profile_class.__name__}: {e}")
        return None

    logger.info(f"Successfully imported {spec.label} data from {file_path}")
    for section in spec.sections:
        logger.debug(f"{section.key} fields: {list(extracted[section.key].keys())}")

    return profile


def compare_with_expected(spec: ImportSpec,
                          imported_data: Dict[str, Dict],
                          expected_profile) -> Dict[str, Dict]:
    """Compare imported section data against an expected profile.

    Returns a dict keyed by section name, each holding 'matches', 'mismatches'
    and 'missing' sub-dicts. A field is only reported missing when the expected
    value is non-empty.
    """
    expected_dict = asdict(expected_profile)
    results = {}

    for section in spec.sections:
        section_expected = expected_dict.get(section.key, {})
        section_actual = imported_data.get(section.key, {})

        matches: Dict[str, Any] = {}
        mismatches: Dict[str, Any] = {}
        missing: Dict[str, Any] = {}

        for field_name, expected_value in section_expected.items():
            if field_name in spec.skip_fields:
                continue

            if field_name in section_actual:
                actual_value = section_actual[field_name]

                if isinstance(actual_value, list) and isinstance(expected_value, list):
                    # Every expected entry must be present, order-insensitive.
                    bucket = matches if set(expected_value).issubset(set(actual_value)) else mismatches
                else:
                    # A non-list expected value may still be a list of
                    # acceptable alternatives; normalize before comparing.
                    expected_list = expected_value if isinstance(expected_value, list) else [expected_value]
                    actual_str = str(actual_value).strip()
                    expected_str_list = [str(e).strip() for e in expected_list]
                    bucket = matches if actual_str in expected_str_list else mismatches

                bucket[field_name] = {'expected': expected_value, 'actual': actual_value}

            elif expected_value:
                missing[field_name] = {'expected': expected_value, 'actual': None}

        results[section.key] = {
            'matches': matches,
            'mismatches': mismatches,
            'missing': missing,
        }

    return results


def validate_file_against_profile(spec: ImportSpec, file_path: str, profile) -> Dict[str, Any]:
    """Validate a tool's JSON output against an expected profile.

    Returns the per-section comparison plus aggregate counts, or an error
    result if the file could not be parsed.
    """
    section_data = spec.parse(file_path)
    if not section_data:
        return {
            'valid': False,
            'error': f"Failed to parse {file_path}",
            'sections': {}
        }

    comparison = compare_with_expected(spec, extract_sections(spec, section_data), profile)

    total_matches = sum(len(s['matches']) for s in comparison.values())
    total_mismatches = sum(len(s['mismatches']) for s in comparison.values())
    total_missing = sum(len(s['missing']) for s in comparison.values())

    return {
        'valid': total_mismatches == 0 and total_missing == 0,
        'file': file_path,
        'total_fields': total_matches + total_mismatches + total_missing,
        'matching_fields': total_matches,
        'sections': comparison
    }
