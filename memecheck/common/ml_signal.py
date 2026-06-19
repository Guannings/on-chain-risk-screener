"""Pluggable ML signal model interface.

Self-review item #3: the current Decider is rule-based with no learned
component. A modest gradient-boosted classifier on the same features
would almost certainly outperform hand-coded rules; but memecheck has a
zero-runtime-dependency promise, so we can't import scikit-learn here.

The compromise: define a stdlib-only abstract interface (`SignalModel`)
that any model (sklearn `GradientBoostingClassifier`, xgboost,
PyTorch...) can be wrapped behind. Users who want the ML layer install
their preferred framework, train a model, write a 10-line adapter that
implements `predict_proba`, and plug it into the Decider via
`Decider.with_signal_model(...)`.

The interface
-------------
A `SignalModel` is anything with:

    predict_proba(features: dict[str, float]) -> float

returning a calibrated probability in [0, 1] that the next window will
contain a rug. The Decider passes its current state's feature snapshot,
combines the score with the rule outputs in a clearly-documented way,
and surfaces the combined verdict.

Feature names exposed to the model
----------------------------------
Stable, document-once contract — any breaking change here is a major
version bump. See `features_from_state` for the canonical extraction.

Combining model + rules
-----------------------
Default behaviour: model agreement is *informational*, not gating.
Rules still drive the action; the model score is logged for offline
calibration. This keeps the existing decision boundary intact and lets
users build trust in the model before promoting it to a co-decider.

A future `SignalCombiner.PROMOTE` mode would let users say "alert iff
both rules AND model agree" — left as a TODO for a real labelled corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from memecheck.monitor.state import MonitorState


# Stable feature contract. ADDING fields is non-breaking; renaming or
# removing is a major-version bump for the model interface.
FEATURE_NAMES: tuple[str, ...] = (
    "liquidity_usd",            # current pool depth in USD
    "liquidity_ratio_vs_l0",    # current / first-observed depth
    "delta_10s_pct",            # signed liquidity change vs 10s ago, %
    "delta_60s_pct",            # signed liquidity change vs 60s ago, %
    "delta_300s_pct",           # signed liquidity change vs 5min ago, %
    "ticks_seen",               # how long the monitor has been watching
)


class SignalModel(Protocol):
    """Minimal interface any model must satisfy.

    Implementations can wrap sklearn / xgboost / PyTorch / anything;
    the contract is just `predict_proba(features) -> float ∈ [0, 1]`.
    """

    def predict_proba(self, features: dict[str, float]) -> float:
        ...


@dataclass(frozen=True)
class ConstantSignalModel:
    """Reference / smoke-test implementation. Returns a fixed probability
    regardless of features. Used in tests and as a baseline."""
    p: float = 0.5

    def predict_proba(self, features: dict[str, float]) -> float:
        return float(self.p)


@dataclass(frozen=True)
class LogisticSignalModel:
    """A toy stdlib-only logistic regression. Inputs are weighted, summed,
    passed through a sigmoid. Useful as a baseline AND as a reminder
    that the framework is plug-compatible with anything trained offline."""
    weights: dict[str, float]
    intercept: float = 0.0

    def predict_proba(self, features: dict[str, float]) -> float:
        import math
        z = self.intercept + sum(
            features.get(k, 0.0) * w for k, w in self.weights.items()
        )
        return 1.0 / (1.0 + math.exp(-z))


def features_from_state(state: MonitorState) -> dict[str, float]:
    """Extract the canonical feature dict. Stable across releases — see
    FEATURE_NAMES module constant."""
    cur = state.current
    base = state.baseline
    liq = cur.liquidity_usd if cur is not None else 0.0
    l0 = base.liquidity_usd if base is not None else 0.0
    ratio = (liq / l0) if l0 > 0 else 0.0
    d10 = state.windowed_delta_pct(10) or 0.0
    d60 = state.windowed_delta_pct(60) or 0.0
    d300 = state.windowed_delta_pct(300) or 0.0
    return {
        "liquidity_usd": float(liq),
        "liquidity_ratio_vs_l0": float(ratio),
        "delta_10s_pct": float(d10),
        "delta_60s_pct": float(d60),
        "delta_300s_pct": float(d300),
        "ticks_seen": float(state.count),
    }


@dataclass(frozen=True)
class SignalVerdict:
    """Result of running a SignalModel + reconciling with the rule output."""
    rule_action: str            # ACTION_NONE / ACTION_ALERT / ACTION_EXECUTE
    model_proba: float          # in [0, 1]
    combined_note: Optional[str]


# Documented decision-band thresholds for the informational reconciliation:
MODEL_BAND_LOW = 0.30           # below this: model is reassuring
MODEL_BAND_HIGH = 0.70          # above this: model is alarmed


def reconcile(rule_action: str, model_proba: float) -> SignalVerdict:
    """Produce a human-readable note describing rule-vs-model agreement.

    Action returned is ALWAYS the rule's action — the model is
    informational, not gating. This keeps the decision boundary stable
    while users gain confidence in the model.
    """
    note: Optional[str] = None
    if rule_action == "ALERT" or rule_action == "EXECUTE":
        if model_proba >= MODEL_BAND_HIGH:
            note = f"rule fired; model agrees ({model_proba:.0%} rug prob)"
        elif model_proba <= MODEL_BAND_LOW:
            note = (
                f"rule fired; model DISAGREES ({model_proba:.0%} rug prob) "
                "— possible false positive"
            )
        else:
            note = f"rule fired; model neutral ({model_proba:.0%} rug prob)"
    else:
        if model_proba >= MODEL_BAND_HIGH:
            note = (
                f"rules quiet but model alarmed ({model_proba:.0%}) "
                "— possible early warning"
            )
        # Rules quiet + model quiet = no note.
    return SignalVerdict(
        rule_action=rule_action,
        model_proba=float(model_proba),
        combined_note=note,
    )
