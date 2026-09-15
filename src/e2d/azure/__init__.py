"""Azure alerting-catalogue (xlsx tracker) -> Dynatrace Davis anomaly detectors.

A customer's Azure monitoring requirement usually arrives as a spreadsheet: one
sheet per Azure service, one row per required monitor, with the metric, the
alert condition in prose, and a severity. This package turns that catalogue into
deployable Settings 2.0 detector objects, and reports what cannot be a detector
(dashboards, workflow triggers) or cannot be built yet (metric not ingesting).
"""

from e2d.azure.model import AzureMonitor, Condition, parse_condition
from e2d.azure.tracker import load_tracker
from e2d.azure.build import build_catalogue, CatalogueResult

__all__ = ["AzureMonitor", "Condition", "parse_condition", "load_tracker",
           "build_catalogue", "CatalogueResult"]
