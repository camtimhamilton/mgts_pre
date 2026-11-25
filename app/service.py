"""Explicit dispatch: integrated ML is default, legacy calculations are demo-only."""
from .config import get_settings


def _implementation():
    if get_settings().analysis_mode == 'demo':
        from . import demo_service
        return demo_service
    from . import ml_service
    return ml_service


def __getattr__(name):
    return getattr(_implementation(), name)
