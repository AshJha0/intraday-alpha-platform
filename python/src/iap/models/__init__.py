"""ML pipeline: baselines -> gated advanced models -> meta-labeling (spec §14)."""

from iap.models.splits import WalkForwardSplitter
from iap.models.dataset import load_dataset, FEATURE_SET, TARGET_COLUMN
from iap.models.pipeline import run_model_comparison, information_coefficient
from iap.models.metalabel import run_meta_labeling

__all__ = [
    "WalkForwardSplitter",
    "load_dataset",
    "FEATURE_SET",
    "TARGET_COLUMN",
    "run_model_comparison",
    "information_coefficient",
    "run_meta_labeling",
]
