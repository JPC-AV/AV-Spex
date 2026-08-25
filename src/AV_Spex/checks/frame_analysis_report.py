"""
Formatting and logging for frame-analysis results.

This is presentation, not analysis: it turns the results dict into the summary
string and the human-readable log blocks. Split out so EnhancedFrameAnalysis is
left doing orchestration.
"""

from typing import Any, Dict, List, Optional, Tuple

from AV_Spex.utils.log_setup import logger
from AV_Spex.checks.frame_geometry import is_valid_active_area

def generate_summary(results: Dict, video_id: str) -> str:
    """Generate comprehensive human-readable summary with specific details"""
    lines = []
    lines.append(f"Enhanced Frame Analysis Summary - {video_id}")
    lines.append("=" * 60)

    # QCTools status
    if results['qctools_report_available']:
        lines.append(f"✓ QCTools report found")
        if 'qctools_violations_found' in results:
            violations = results['qctools_violations_found']
            if isinstance(violations, str):
                lines.append(f"  {violations}")
            else:
                lines.append(f"  Frames with BRNG > 0: {violations}")

    # Border detection with frame dimensions
    if 'initial_borders' in results:
        borders = results.get('final_borders', results['initial_borders'])
        if is_valid_active_area(borders['active_area']):
            x, y, w, h = borders['active_area']
            lines.append(f"\nBorder Detection:")
            lines.append(f"  Active area: {w}x{h} at ({x},{y})")
            lines.append(f"  Method: {borders['detection_method']}")
            # Calculate border widths
            frame_w, frame_h = 720, 486  # Standard NTSC
            left_border = x
            right_border = frame_w - (x + w)
            top_border = y
            bottom_border = frame_h - (y + h)
            lines.append(f"  Borders: L:{left_border}px R:{right_border}px T:{top_border}px B:{bottom_border}px")

    # BRNG analysis statistics
    if results.get('brng_analysis'):
        brng = results.get('final_brng_analysis', results['brng_analysis'])
        stats = brng.get('actionable_report', {}).get('summary_statistics', {})
        aggregate = brng.get('aggregate_patterns', {})

        lines.append(f"\nBRNG Analysis:")
        lines.append(f"  Frames analyzed: {stats.get('total_violations', 0)}")
        lines.append(f"  Average BRNG: {stats.get('average_violation_percentage', 0):.2f}%")
        lines.append(f"  Maximum BRNG: {stats.get('max_violation_percentage', 0):.2f}%")
        edge_pct = aggregate.get('edge_violation_percentage', 0)
        continuous_pct = aggregate.get('continuous_edge_percentage', 0)
        lines.append(f"  Edge violations (any): {edge_pct:.1f}% of analyzed frames")
        lines.append(f"  Edge violations (solid line): {continuous_pct:.1f}% of analyzed frames")
        if continuous_pct == 0 and edge_pct > 95:
            lines.append(f"    → Violations are scattered rather than forming a solid line")

        # Diagnostic breakdown
        if brng.get('violations'):
            diagnostic_counts = {}
            for v in brng['violations']:
                if isinstance(v, dict) and v.get('diagnostics'):
                    for diag in v['diagnostics']:
                        if diag.startswith("Edge artifacts"):
                            diagnostic_counts["Edge artifacts"] = diagnostic_counts.get("Edge artifacts", 0) + 1
                        elif diag != "Border adjustment recommended":
                            diagnostic_counts[diag] = diagnostic_counts.get(diag, 0) + 1

            if diagnostic_counts:
                lines.append(f"  Violation types:")
                for diag, count in sorted(diagnostic_counts.items(), key=lambda x: x[1], reverse=True):
                    lines.append(f"    {diag}: {count} frames")

    # Signalstats comparison
    if results.get('signalstats'):
        stats = results['signalstats']
        lines.append(f"\nSignalstats (active area analysis):")
        lines.append(f"  Frames with violations: {stats['violation_percentage']:.1f}%")
        lines.append(f"  Max BRNG: {stats['max_brng']:.2f}%")
        lines.append(f"  Avg BRNG: {stats['avg_brng']:.2f}%")

        # Add analysis period info
        periods = stats.get('analysis_periods', [])
        if periods:
            period_strs = [f"{p[0]:.1f}s-{p[0]+p[1]:.1f}s" for p in periods]
            lines.append(f"  Analysis periods: {', '.join(period_strs)}")

    # Refinement info
    if 'refinement_iterations' in results and results['refinement_iterations'] > 0:
        lines.append(f"\nBorder refinement performed: {results['refinement_iterations']} iteration(s)")
        if 'initial_borders' in results and 'final_borders' in results:
            initial = results['initial_borders']['active_area']
            final = results['final_borders']['active_area']
            if initial and final:
                w_change = final[2] - initial[2]
                h_change = final[3] - initial[3]
                lines.append(f"  Size change: width {w_change:+d}px, height {h_change:+d}px")

    return "\n".join(lines)


def log_brng_analysis_summary(brng_results: 'BRNGAnalysisResult', 
                            analysis_periods: List[Tuple[float, int]]) -> None:
    """Log a comprehensive summary of BRNG analysis results."""
    if not brng_results or not brng_results.violations:
        logger.info("\n  === BRNG Frame Analysis Summary ===")
        logger.info("  No BRNG violations detected in analyzed periods.\n")
        return

    violations = brng_results.violations
    aggregate = brng_results.aggregate_patterns
    report = brng_results.actionable_report
    stats = report.get('summary_statistics', {})

    # Calculate time range
    if analysis_periods:
        time_start = min(p[0] for p in analysis_periods)
        time_end = max(p[0] + p[1] for p in analysis_periods)
        time_range_str = f"{time_start:.1f}s - {time_end:.1f}s"
    else:
        time_range_str = "N/A"

    logger.info(f"\n  === BRNG Frame Analysis Summary ===")
    logger.info(f"  Analyzed {len(violations)} frames across {len(analysis_periods)} periods ({time_range_str})\n")

    # Aggregate diagnostic types from all violations
    diagnostic_counts = {}
    edge_artifact_edges = set()

    for v in violations:
        if v.diagnostics:
            for diag in v.diagnostics:
                # Normalize edge artifact messages
                if diag.startswith("Edge artifacts"):
                    diagnostic_counts["Edge artifacts"] = diagnostic_counts.get("Edge artifacts", 0) + 1
                    # Extract edge names from message like "Edge artifacts (left, right)"
                    if "(" in diag and ")" in diag:
                        edges_str = diag[diag.find("(")+1:diag.find(")")]
                        for edge in edges_str.split(", "):
                            edge_artifact_edges.add(edge.strip())
                elif diag == "Border adjustment recommended":
                    continue
                else:
                    diagnostic_counts[diag] = diagnostic_counts.get(diag, 0) + 1

    # Log diagnostic types
    logger.info("  Violation Types Detected:")
    total_violations = len(violations)

    # Order diagnostics by relevance
    priority_order = ["Sub-black detected", "Highlight clipping", "Edge artifacts", 
                    "Linear blanking patterns", 
                    "General broadcast range violations"]

    logged_any = False
    for diag_type in priority_order:
        if diag_type in diagnostic_counts:
            count = diagnostic_counts[diag_type]
            pct = (count / total_violations) * 100

            if diag_type == "Edge artifacts" and edge_artifact_edges:
                edges_str = ", ".join(sorted(edge_artifact_edges))
                logger.info(f"    • {diag_type} ({edges_str}): {count} frames ({pct:.1f}%)")
            else:
                logger.info(f"    • {diag_type}: {count} frames ({pct:.1f}%)")
            logged_any = True

    # Log any remaining diagnostics not in priority order
    for diag_type, count in diagnostic_counts.items():
        if diag_type not in priority_order:
            pct = (count / total_violations) * 100
            logger.info(f"    • {diag_type}: {count} frames ({pct:.1f}%)")
            logged_any = True

    if not logged_any:
        logger.info("    • No specific diagnostic patterns identified")

    # Add warning if edge percentage is high
    edge_pct = aggregate.get('edge_violation_percentage', 0)
    if edge_pct > 50:
        logger.info(f"    ⚠ High edge percentage ({edge_pct:.1f}%) suggests border detection needs adjustment")

   # Log violation distribution statistics
    logger.info(f"\n  Violation Statistics:")
    logger.info(f"    Average BRNG: {stats.get('average_violation_percentage', 0):.2f}%")
    logger.info(f"    Maximum BRNG: {stats.get('max_violation_percentage', 0):.2f}%")
    continuous_pct = aggregate.get('continuous_edge_percentage', 0)
    logger.info(f"    Edge violations (any): {edge_pct:.1f}% of analyzed frames")
    logger.info(f"    Edge violations (solid line): {continuous_pct:.1f}% of analyzed frames")
    if continuous_pct == 0 and edge_pct > 95:
        logger.info(f"      → Violations are scattered rather than forming a solid line")

    linear_pct = aggregate.get('linear_pattern_percentage', 0)
    if linear_pct > 0:
        logger.info(f"    Linear patterns: {linear_pct:.1f}% of analyzed frames")

    logger.info("")  # Blank line for spacing


def log_analysis_correlation(signalstats_results: 'SignalstatsResult',
                            brng_results: 'BRNGAnalysisResult') -> None:
    """Log the correlation between signalstats and BRNG analysis results."""
    if not signalstats_results or not brng_results:
        return

    logger.info("  === Analysis Correlation ===\n")

    # Signalstats summary
    logger.info("  Signalstats (quantitative full-frame vs active-area comparison):")
    logger.info(f"    Active area violations: {signalstats_results.violation_percentage:.1f}% of frames")
    logger.info(f"    Max BRNG in active area: {signalstats_results.max_brng:.2f}%")
    logger.info(f"    Diagnosis: {signalstats_results.diagnosis}\n")

    # BRNG analysis summary
    aggregate = brng_results.aggregate_patterns
    edge_pct = aggregate.get('edge_violation_percentage', 0)

    # Determine dominant diagnostic from violations
    diagnostic_counts = {}
    for v in brng_results.violations:
        if v.diagnostics:
            for diag in v.diagnostics:
                if diag.startswith("Edge artifacts"):
                    diag = "Edge artifacts"
                elif diag == "Border adjustment recommended":
                    continue  # Skip meta-diagnostics
                diagnostic_counts[diag] = diagnostic_counts.get(diag, 0) + 1

    dominant_diag = max(diagnostic_counts.items(), key=lambda x: x[1])[0] if diagnostic_counts else "Unknown"

    logger.info("  BRNG Analysis (qualitative frame inspection):")
    continuous_pct = aggregate.get('continuous_edge_percentage', 0)
    logger.info(f"    Edge violations (any): {edge_pct:.1f}%")
    logger.info(f"    Edge violations (solid line): {continuous_pct:.1f}%")
    if continuous_pct == 0 and edge_pct > 95:
        logger.info(f"      → Violations are scattered rather than forming a solid line")
    logger.info(f"    Dominant diagnostic: {dominant_diag}\n")

    # Interpretation
    logger.info("  Interpretation:")

    # Determine agreement between methods
    signalstats_says_border = "border" in signalstats_results.diagnosis.lower()
    signalstats_says_content = "active" in signalstats_results.diagnosis.lower() and "requires" in signalstats_results.diagnosis.lower()
    brng_says_border = brng_results.requires_border_adjustment or edge_pct > 50
    brng_says_content = not brng_says_border and edge_pct < 30

    if signalstats_says_border and brng_says_border:
        logger.info("    ✓ Both methods agree: violations are concentrated at frame edges")
        logger.info("      → Border detection likely missed some blanking areas")
        logger.info("      → Active picture content appears broadcast-safe once borders are corrected\n")
    elif signalstats_says_content and brng_says_content:
        logger.info("    ✓ Both methods agree: violations are in the active picture area")
        logger.info("      → Content itself has broadcast range issues")
        logger.info("      → Review source material or encoding parameters\n")
    elif signalstats_says_content and brng_says_border:
        logger.info("    ⚠ Methods show mixed results:")
        logger.info("      → Signalstats: active area has violations")
        logger.info("      → BRNG analysis: high edge violation percentage")
        logger.info("      → Both content issues and border detection may need attention\n")
    elif signalstats_says_border and brng_says_content:
        logger.info("    ⚠ Methods show mixed results:")
        logger.info("      → Signalstats: border areas have more violations")
        logger.info("      → BRNG analysis: violations spread throughout frame")
        logger.info("      → Review thumbnails to determine actual issue location\n")
    else:
        # Default case
        if edge_pct > 50:
            logger.info("    → High edge violation percentage suggests border issues")
        elif edge_pct < 20:
            logger.info("    → Low edge percentage suggests content-based violations")
        else:
            logger.info("    → Mixed violation distribution - review thumbnails for details")
        logger.info("")


def format_refinement_comparison_text(border_detector, initial_borders, final_borders,
                                      initial_brng, final_brng, refinement_history):
    """
    Format the comparison text for the refinement visualization.

    Returns:
        Formatted string with comparison details
    """
    x1, y1, w1, h1 = initial_borders.active_area
    x2, y2, w2, h2 = final_borders.active_area

    # Calculate changes
    width_change = w2 - w1
    height_change = h2 - h1
    left_change = x2 - x1
    right_change = (border_detector.width - (x2 + w2)) - (border_detector.width - (x1 + w1))
    top_change = y2 - y1
    bottom_change = (border_detector.height - (y2 + h2)) - (border_detector.height - (y1 + h1))

    # BRNG violations
    initial_violations = len(initial_brng.violations) if initial_brng.violations else 0
    final_violations = len(final_brng.violations) if final_brng.violations else 0
    violation_reduction = initial_violations - final_violations

    # Build text
    lines = []
    lines.append(f"Active Area: {w1}x{h1} → {w2}x{h2} (Δ width: {width_change:+d}px, Δ height: {height_change:+d}px)")

    # Border changes
    border_changes = []
    if abs(left_change) > 2:
        direction = "expanded" if left_change < 0 else "contracted"
        border_changes.append(f"Left {direction} {abs(left_change)}px")
    if abs(right_change) > 2:
        direction = "expanded" if right_change > 0 else "contracted"
        border_changes.append(f"Right {direction} {abs(right_change)}px")
    if abs(top_change) > 2:
        direction = "expanded" if top_change < 0 else "contracted"
        border_changes.append(f"Top {direction} {abs(top_change)}px")
    if abs(bottom_change) > 2:
        direction = "expanded" if bottom_change > 0 else "contracted"
        border_changes.append(f"Bottom {direction} {abs(bottom_change)}px")

    if border_changes:
        lines.append(f"Border Changes: {', '.join(border_changes)}")
    else:
        lines.append("Border Changes: None")

    # Violation improvement
    lines.append(f"BRNG Violations: {initial_violations} → {final_violations} ({violation_reduction:+d})")

    if initial_violations > 0:
        improvement_pct = (violation_reduction / initial_violations) * 100
        lines.append(f"Improvement: {improvement_pct:.1f}%")

    # Edge violation percentages
    initial_edge_pct = initial_brng.aggregate_patterns.get('edge_violation_percentage', 0)
    final_edge_pct = final_brng.aggregate_patterns.get('edge_violation_percentage', 0)
    lines.append(f"Edge Violations: {initial_edge_pct:.1f}% → {final_edge_pct:.1f}%")

    return " | ".join(lines)
