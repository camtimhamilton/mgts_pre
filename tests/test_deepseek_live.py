import asyncio

import pytest

from scripts.smoke_deepseek import run_smoke, skip_reason


@pytest.mark.live
def test_real_deepseek_synthetic_smoke():
    reason = skip_reason()
    if reason:
        pytest.skip(reason)
    report = asyncio.run(run_smoke())
    assert report["status"] == "passed", report
    assert report["provider"] == "deepseek" and report["calls"]
