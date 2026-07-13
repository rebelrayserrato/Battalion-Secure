"""Cross-Task risk dashboard.

Read-only aggregation of findings already stored by the review pipeline
(including Isolation-Forest fraud signals) across every Task/matter.

The aggregation layer (:mod:`review_engine.dashboard.aggregation`) is pure
standard-library Python so it can be unit-tested without the heavier app
dependencies. Rendering (Streamlit/Altair) lives in
:mod:`review_engine.dashboard.view`.
"""
