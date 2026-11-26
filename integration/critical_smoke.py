"""Check the final XLSX, local form delivery and scenario in Edge without network calls."""
import argparse
import json
import os
from collections import Counter
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.pair_import import import_history
from .smoke import ROOT, running_stack


def run(mentor_path, intern_path):
    fixture = json.loads((ROOT / "ml_service/tests/fixtures/pair_expert_synthetic_text.json").read_text("utf-8"))
    with running_stack(text_mode="deepseek", pair_policy="expert", browser_fixture=True, timeout=25) as stack:
        client, url, _, temporary, _ = stack

        def api(path, body=None):
            response = client.get(url + "/api/v2/" + path) if body is None else client.post(url + "/api/v2/" + path, json=body)
            response.raise_for_status()
            return response.json()

        from app.config import get_settings
        original_path = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = str(temporary / "backend.sqlite3")
        get_settings.cache_clear()
        try:
            assert import_history(mentor_path, intern_path)["accepted"] == 60
        finally:
            if original_path is None:
                os.environ.pop("DB_PATH")
            else:
                os.environ["DB_PATH"] = original_path
            get_settings.cache_clear()
        history = api("analytics", {})
        assert history["methodology"]["version"] == "1.1.0-draft"
        expected = {"x": 7.808333333333334, "y": 10.766666666666667, "z": 9.2875}
        for k, number in expected.items():
            assert abs(history["indices"][k] - number) < 1e-8
        assert Counter(m[side]["band"] for row in history["matrix"] for m in row["months"] for side in "xy") == {"green": 53, "yellow": 7}
        row = next(row for row in history["matrix"] if row["intern_id"].endswith("003"))
        assert row["months"][2]["actual"] == {"x": 36, "y": 52.5, "z": 44.25}
        assert all(not side["nlp_audit"]["calls"] for row_data in history["matrix"]
                   for cell in row_data["months"] for side in (cell["x"], cell["y"]))

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            def cell(pair_id, month):
                return page.locator(f'.cell[data-pair="{pair_id}"][data-month="{month}"]')

            def mode(value):
                page.locator(f'[data-mode="{value}"]').click()
                expect(page.locator('#connection[data-state="ready"]')).to_be_visible()

            page.goto(url)
            expect(page.locator("#view-label")).to_have_text("Предварительный расчёт по оценкам")
            expect(page.locator(".cell")).to_have_count(30)
            expect(page.locator("#recalculate, #manage")).to_have_count(0)
            mode("PAIR_MONTH")
            cell(row["pair_id"], 3).click()
            expect(page.locator('[data-index="z"] b')).to_have_text("44,25")
            page.locator('[data-index="z"]').click()
            expect(page.locator("#dialog-body")).to_contain_text("Z =")
            expect(page.locator("#dialog-body .detail-tree > details > details > details")).to_have_count(2)
            page.locator("#close").click()
            expect(page.locator('[data-section="pair"] input[data-action="P04"]')).to_be_visible()
            expect(page.locator('[data-section="mentor"] input[data-action="M_A03"]')).to_be_visible()
            expect(page.locator('[data-section="intern"] input[data-action="S_A01"]')).to_be_visible()
            expect(page.locator('[data-section="intern"] input[data-action="S_A02"]')).to_be_visible()
            page.locator('input[data-action="S_A01"]').check()
            expect(cell(row["pair_id"], 3).locator(".half").nth(1)).to_have_class("half yellow")
            page.locator('input[data-action="S_A02"]').check()
            expect(cell(row["pair_id"], 3).locator(".half").nth(1)).to_have_class("half green")
            page.locator("#reset").click()
            expect(page.locator('[data-index="z"] b')).to_have_text("44,25")
            expect(cell(row["pair_id"], 3).locator(".half").nth(1)).to_have_class("half yellow")
            workload = next(r for r in history["matrix"] if r["intern_id"].endswith("004"))
            cell(workload["pair_id"], 4).click()
            expect(page.locator('[data-section="intern"] input[data-action="S_A04"]')).to_be_visible()
            expect(page.locator('[data-section="intern"]')).to_contain_text("Согласовать объём нагрузки")
            expect(page.get_by_text("Уточнить неоднозначный комментарий конфиденциально")).to_have_count(0)
            mode("PAIR_RANGE")
            communication = next(r for r in history["matrix"] if r["intern_id"].endswith("005"))
            cell(communication["pair_id"], 1).click()
            cell(communication["pair_id"], 3).click()
            expect(page.locator(".trend-month")).to_have_count(3)
            expect(page.locator('[data-section="pair"] input[data-action="P02"]')).to_be_visible()
            expect(page.locator('[data-section="intern"] input[data-action="S_A01"]')).to_be_visible()
            mode("GLOBAL")

            identity = {"intern_id": "synthetic-final-intern", "mentor_id": "synthetic-final-mentor", "department": "Учебный макет"}
            mentor = {**identity, "role": "mentor", "month": "М1",
                      "ratings": dict.fromkeys(("clarity", "quality", "autonomy", "deadlines", "initiative", "team"), 5)}
            intern = {**identity, **fixture, "month": 1,
                      "ratings": {"clarity": 1, "workload": 1, "interest": 1, "growth": 1, "support": 5, "team": 5, "satisfaction": 5}}

            def deliver(form):
                return api("forms/yandex", {"jsonrpc": "2.0", "method": "submit", "id": 1,
                           "params": {**form, "form_id": "synthetic-final-" + form["role"], "response_id": 1}})["result"]

            pair_id = deliver(mentor)["pair_id"]
            receipt = deliver(intern)
            assert receipt["status"] == "accepted" and deliver(intern)["status"] == "duplicate"
            expect(cell(pair_id, 1).locator(".half").nth(1)).to_have_class("half red")
            page.locator("#update").click()
            expect(page.locator("#dialog-body").get_by_text("Ответ получен")).to_have_count(2)
            assert all(f["accepted"] == 1 for f in api("forms/status")["forms"])
            page.locator("#close").click()
            mode("PAIR_MONTH")
            cell(pair_id, 1).click()
            expect(page.locator("#view-label")).to_contain_text("AI-анализ")
            action = page.locator('[data-section="intern"] input[data-action="S_A02"]')
            with page.expect_response(lambda response: "/analytics?preview=true" in response.url) as pending:
                action.check()
            expect(cell(pair_id, 1).locator(".half").nth(1)).to_have_class("half yellow")
            expect(page.locator('[data-index="y"]')).to_contain_text("снижение")
            preview_id = pending.value.json()["analysis_id"]
            assert client.get(url + "/api/v2/analysis/" + preview_id).status_code == 404
            assert api("scenarios") == []
            page.reload()
            expect(cell(pair_id, 1).locator(".half").nth(1)).to_have_class("half red")
            action.check()
            expect(cell(pair_id, 1).locator(".half").nth(1)).to_have_class("half yellow")
            page.locator("#reset").click()
            expect(cell(pair_id, 1).locator(".half").nth(1)).to_have_class("half red")
            assert not errors, errors
            browser.close()
        return {"historical_indices": expected, "forms": 2, "provider": "offline_browser_fixture", "browser": "passed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mentor", required=True, type=Path)
    parser.add_argument("--intern", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.mentor, args.intern)))
