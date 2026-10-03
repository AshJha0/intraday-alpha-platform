"""ML pipeline: baselines -> gated advanced models -> meta-labeling (spec §14)."""

from iap.models.dataset import FEATURE_SET, TARGET_COLUMN, load_dataset
from iap.models.metalabel import run_meta_labeling
from iap.models.pipeline import information_coefficient, run_model_comparison
from iap.models.splits import WalkForwardSplitter

__all__ = [
    "WalkForwardSplitter",
    "load_dataset",
    "FEATURE_SET",
    "TARGET_COLUMN",
    "run_model_comparison",
    "information_coefficient",
    "run_meta_labeling",
]
