"""Exercise the pair webhook-to-dashboard path on allowlisted synthetic text."""
import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from .smoke import ROOT, running_stack


def load_key():
    if os.getenv("DEEPSEEK_API_KEY"):
        return
    for path in (ROOT / "ml_service/.env", ROOT / ".env"):
        if path.is_file():
            entries = (line.strip().removeprefix("export ").partition("=")
                       for line in path.read_text("utf-8-sig").splitlines())
            key = next((value.strip().strip("'\042") for name, sep, value in entries
                        if sep and name.strip() == "DEEPSEEK_API_KEY"), "")
            if key:
                os.environ["DEEPSEEK_API_KEY"] = key
                return


def run(live=False, screenshot=None):
    if live:
        load_key()
        if not os.getenv("DEEPSEEK_API_KEY"):
            raise SystemExit("Live DeepSeek smoke blocked: DEEPSEEK_API_KEY is absent")
    fixture = json.loads((ROOT / "ml_service/tests/fixtures/pair_synthetic_text.json").read_text("utf-8"))
    intern_id = "synthetic-ds-intern"
    identity = {"intern_id": intern_id, "mentor_id": "synthetic-ds-mentor", "department": "Учебный макет"}
    with running_stack(text_mode="deepseek", pair_policy="expert", live_deepseek=live, timeout=100) as stack:
        client, base, *_ = stack
        registration = client.post(base + "/api/v2/pairs", json=identity)
        registration.raise_for_status()
        mentor = {**identity, "role": "mentor", "month": 1, "ratings": dict.fromkeys(
            ("clarity", "quality", "autonomy", "deadlines", "initiative", "team"), 5)}
        receipt = client.post(base + "/api/v2/ingest", json=mentor, headers={"X-Delivery-Id": "synthetic-ds-mentor"})
        receipt.raise_for_status()
        body = {"jsonrpc": "2.0", "method": "submit", "id": 1, "params": {
            **identity, **fixture, "form_id": "synthetic-ds", "response_id": "1", "month": 1,
            "ratings": dict.fromkeys(("clarity", "support", "workload", "interest", "growth", "team", "satisfaction"), 5)}}
        response = client.post(base + "/api/v2/forms/yandex", json=body)
        response.raise_for_status()
        assert response.json()["result"]["status"] == "accepted"
        analysis = client.post(base + "/api/v2/analytics", json={})
        analysis.raise_for_status()
        data = analysis.json()
        cell = data["matrix"][0]["months"][0]
        assert client.get(base + "/api/v2/analysis/" + data["analysis_id"]).json() == data
        assert client.get(base + "/api/v2/source/" + response.json()["result"]["source_id"]).status_code == 200
        if live:
            assert cell["y"]["text_analysis_status"] == "completed", {
                key: cell["y"]["nlp_audit"].get(key) for key in ("error", "schema_errors")}
            assert cell["y"]["nlp_audit"]["calls"] and cell["eligible"]
        else:
            assert cell["y"]["status"] == "partial" and cell["y"]["score"] == 0
            assert cell["y"]["band"] == "gray" and cell["z"] is None and not cell["eligible"]
            assert not cell["y"]["signals"]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.goto(base)
            expect(page.locator('.cell[data-month="1"] .half').nth(1)).to_have_class("half " + cell["y"]["band"])
            if not live:
                expect(page.locator("#message")).to_contain_text("Текстовый анализ завершился с ошибкой")
            if screenshot:
                dest = Path(screenshot).resolve()
                if not dest.is_relative_to(ROOT / "data"):
                    raise ValueError("Use a screenshot path under data/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=dest)
            browser.close()
        return {"mode": "live" if live else "missing_key", "status": cell["y"]["text_analysis_status"],
                "model": cell["y"]["nlp_audit"]["calls"][0]["requested_model"] if live else None,
                "signals": len(cell["y"]["signals"]), "paired": cell["eligible"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    print(json.dumps(run(live=args.live, screenshot=args.screenshot), ensure_ascii=False))
