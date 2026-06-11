"""Campaign lifecycle, evaluation metrics, and the promotion report
(Sections 8, 15)."""

from hermes_pm.campaign.evaluation import CampaignEvaluator
from hermes_pm.campaign.manager import CampaignManager
from hermes_pm.campaign.promotion import build_promotion_report

__all__ = ["CampaignManager", "CampaignEvaluator", "build_promotion_report"]
