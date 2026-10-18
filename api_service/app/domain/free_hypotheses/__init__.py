"""Free hypothesis search outside the Matrix (TZ 9.5, module priority High).

Four approaches (LOGICAL_ANALYSIS, SEMANTIC_DISSONANCE, NORMATIVE_ANALYSIS, ML pattern analysis) plus the recall-oriented
UNMATCHED_DIFF ("a discrepancy was found but no matrix parameter describes it") and the page-text detectors of step R
(CHANGE_SHEET: a change the RD register records that the PD still shows in its earlier state; LAYER_COMPOSITION: layer build-ups
of roofs / floors / walls compared PD vs RD). A result is a hypothesis
(`finding_status = SUSPICION`): not a violation, not counted in the violation totals, not a training label. The inspector
reviews it, or converts it to a CANDIDATE when concrete sources and boxes exist. See `runner.py` for the entry point and
`evaluation/phase12/FINAL_B2_HYPOTHESES_REPORT.md` for the measurements and the default switch.
"""
from .base import (  # noqa: F401
    ALL_METHODS,
    FREE_SEARCH_PARAM_CODE,
    METHOD_CHANGE_SHEET,
    METHOD_LAYERS,
    METHOD_LOGICAL,
    METHOD_ML,
    METHOD_NORMATIVE,
    METHOD_SEMANTIC,
    METHOD_UNMATCHED,
    EvidenceRef,
    HypothesisCandidate,
    enabled_methods,
    free_hypotheses_enabled,
)
