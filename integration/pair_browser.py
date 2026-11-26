"""Real headless Edge acceptance flow against ephemeral v2 services and XLSX.

Corporate screenshots and reports are restricted to ignored data/review/.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from .pair_history_checks import check_history
from .smoke import ROOT, running_stack, wait_until


def run(mentor_path, intern_path, destination, text_mode="local_rules", expert_baseline=None):
    destination = Path(destination).resolve()
    if not destination.is_relative_to((ROOT / "data").resolve()):
        raise ValueError("confidential screenshots must stay under ignored data/")
    destination.mkdir(parents=True, exist_ok=True)
    checks = []
    with running_stack(text_mode=text_mode, timeout=60, pair_policy="expert_v1" if expert_baseline else "legacy") as stack:
        client, base, ml_base, temp, processes = stack
        env = dict(os.environ, DB_PATH=str(temp / "backend.sqlite3"), PYTHONIOENCODING="utf-8")
        command = [sys.executable, "-m", "tools.import_pair_history", "--mentor", str(mentor_path), "--intern", str(intern_path)]
        first = subprocess.run(command, cwd=ROOT, env=env, check=True, capture_output=True, text=True, encoding="utf-8")
        assert json.loads(first.stdout)["accepted"] == 60
        second = subprocess.run(command, cwd=ROOT, env=env, check=True, capture_output=True, text=True, encoding="utf-8")
        assert json.loads(second.stdout)["duplicates"] == 60
        history = check_history(mentor_path, intern_path, client, base)
        (destination / "history-checks.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        checks.extend(["XLSX import 60 records and repeat 60 duplicates", "all saved answers equal original XLSX", "eight data-driven historical situation types"])
        response = wait_until(lambda: (r if (r := client.post(base + "/api/v2/analytics", json={})).status_code == 200 else None))
        response.raise_for_status()
        initial = response.json()
        assert len(initial["matrix"]) == 5
        registry = {r["intern_id"]: r["pair_id"] for r in initial["matrix"]}
        for report in ([] if expert_baseline else history["cases"]["explicit_resolution"]):
            assert any(p["type"] == "confirmed_resolution" and p["pair_id"] == registry[report["intern_id"]]
                       and p["month"] == report["month"] and any(q["quote"] in report["quote"] for q in p["evidence"])
                       for p in initial["patterns"])
        for gap in history["cases"]["task_perception_gap"]:
            assert any(d["pair_id"] == registry[gap["intern_id"]] and d["month"] == gap["month"]
                       and d["aspect"] == ("M01" if expert_baseline else "clarity") and (d["mentor_rating"],d["intern_rating"]) == (gap["mentor"],gap["intern"])
                       for d in initial["discrepancies"])
        checks.append("Numeric gap evidence matches original XLSX cases" +
                      ("" if expert_baseline else "; explicit ML resolution checked"))
        if expert_baseline:
            reference = json.loads(Path(expert_baseline).read_text("utf-8"))["observations"]
            actual = {(f'{r["mentor_id"]}/{r["intern_id"]}', m["month"]): m["actual"]
                      for r in initial["matrix"] for m in r["months"]}
            assert len(reference) == len(actual) == 30
            for item in reference:
                assert actual[(item["pair"], item["period"])] == {k: item[k.upper()+"_rating"] for k in "xyz"}
            checks.append("30 historical rating-only baseline rows agree with the expert pack")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1680, "height": 1100})
            page.set_default_timeout(15000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def ready():
                expect(page.locator('#connection[data-state="ready"]')).to_be_visible()

            def mode(value):
                page.locator(f'[data-mode="{value}"]').click()
                ready()
                expect(page.locator(f'[data-mode="{value}"]')).to_have_attribute("aria-pressed", "true")

            def cell(pair_id, month):
                return page.locator(f'.cell[data-pair="{pair_id}"][data-month="{month}"]')

            def click_cell(pair_id, month):
                cell(pair_id, month).click()
                ready()

            def session():
                return page.evaluate('JSON.parse(sessionStorage.getItem("pulse-pair-session"))')

            def screenshot(name):
                page.evaluate("window.scrollTo(0,0)")
                page.wait_for_timeout(100)
                page.screenshot(path=destination / name, full_page=True)

            page.goto(base + "/")
            ready()
            expect(page.locator("#matrix tbody tr")).to_have_count(5)
            expect(page.locator(".cell")).to_have_count(30)
            expect(page.locator(".cell .half")).to_have_count(60)
            assert page.locator("#sections > article").evaluate_all("els=>els.map(e=>e.dataset.section)") == ["pair", "mentor", "intern"]
            for row in initial["matrix"]:
                for month in row["months"]:
                    halves = cell(row["pair_id"], month["month"]).locator(".half")
                    expect(halves.nth(0)).to_have_class("half " + month["x"]["band"])
                    expect(halves.nth(1)).to_have_class("half " + month["y"]["band"])
            assert page.locator(".cell:enabled").count() == 0
            expect(page.locator(".mgts-logo")).to_be_visible()
            assert page.locator(".mgts-logo").evaluate("(img) => img.complete && img.naturalWidth > 0")
            expect(page.locator(".trend-month")).to_have_count(6)
            screenshot("pair-global.png")
            checks.extend(["GLOBAL default and disabled cells", "5 by 6 matrix and 60 correctly colored halves", "three sections in required order"])

            candidates = sorted(((r, m) for r in initial["matrix"] for m in r["months"]),
                                key=lambda item: (item[1]["x"]["score"] or 0) + (item[1]["y"]["score"] or 0), reverse=True)
            row, month = candidates[0]
            pair_id, selected_month = row["pair_id"], month["month"]
            mode("PAIR_MONTH")
            click_cell(pair_id, selected_month)
            assert session()["scope"] == {"type": "PAIR_MONTH", "pair_id": pair_id, "start": selected_month, "end": selected_month}
            expect(page.locator('.cell[data-active="true"]')).to_have_count(1)
            for key in ("x", "y", "z"):
                page.locator(f'[data-index="{key}"]').click()
                expect(page.locator("#dialog")).to_be_visible()
                expect(page.locator("#dialog-body")).to_contain_text("Фактически")
                if key == "z":
                    expect(page.locator("#dialog-body")).to_contain_text("Z =")
                page.locator("#close").click()
            page.locator('[data-section="intern"] > details > summary').filter(has_text="Подробнее").click()
            page.locator('[data-section="intern"] summary').filter(has_text="Цитаты из анкет").click()
            page.locator('[data-section="intern"] .quote').first.click()
            expect(page.locator("#dialog mark")).to_be_visible()
            expect(page.locator("#dialog-title")).to_contain_text("Стажёр")
            page.locator("#close").click()
            for role in ("mentor", "intern"):
                if role=="mentor":
                    page.locator(f'[data-section="{role}"] > details > summary').filter(has_text="Подробнее").click()
                page.locator(f'[data-section="{role}"] summary').filter(has_text="Динамика исходных оценок").click()
                expect(page.locator(f'[data-section="{role}"] table.data')).to_be_visible()
            screenshot("pair-month.png")
            checks.extend(["PAIR_MONTH selection", "X Y Z index detail", "clickable source and highlighted exact quote", "raw 1–5 dynamics for both roles"])

            mode("PAIR_RANGE")
            click_cell(pair_id, 5)
            click_cell(pair_id, 2)
            assert session()["scope"]["start"] == 2 and session()["scope"]["end"] == 5
            expect(page.locator('.cell[data-active="true"]')).to_have_count(4)
            expect(page.locator(".trend-month")).to_have_count(4)
            click_cell(pair_id, 1)
            other_pair = next(r["pair_id"] for r in initial["matrix"] if r["pair_id"] != pair_id)
            click_cell(other_pair, 6)
            assert session()["scope"] == {"type": "PAIR_RANGE", "pair_id": other_pair, "start": 6, "end": 6}
            expect(page.locator('.cell[data-active="true"]')).to_have_count(1)
            # New anchor on another row, then click the same cell to close it.
            click_cell(pair_id, selected_month)
            click_cell(pair_id, selected_month)
            range_result = client.post(base + "/api/v2/analytics", json={"scope": session()["scope"]}).json()
            month_result = client.post(base + "/api/v2/analytics", json={"scope": {**session()["scope"], "type": "PAIR_MONTH"}}).json()
            assert range_result["indices"] == month_result["indices"]
            checks.extend(["reverse horizontal range 5 to 2", "cross-row range prevented", "one-month range equals PAIR_MONTH"])

            mode("PAIR_MONTH")
            click_cell(pair_id, selected_month)
            actual = client.post(base + "/api/v2/analytics", json={"scope": session()["scope"]}).json()
            original_colors = cell(pair_id, selected_month).locator(".half").evaluate_all("els=>els.map(e=>e.className)")
            first_section = "intern" if expert_baseline else "pair"
            checkbox = page.locator(f'[data-section="{first_section}"] input[type="checkbox"]').first
            expect(checkbox).to_be_visible()
            action_id = checkbox.get_attribute("data-action")
            checkbox.check()
            expect(page.locator('#metrics .metric').first).to_contain_text("После улучшений")
            ready()
            assert len(session()["selected"]) == 1
            scenario = client.post(base + "/api/v2/analytics", json={"scope": session()["scope"], "selected": session()["selected"]}).json()
            if expert_baseline:
                assert scenario["indices"]["x"] == actual["indices"]["x"]
            else:
                assert scenario["indices"]["x"] < actual["indices"]["x"]
            assert scenario["indices"]["y"] < actual["indices"]["y"]
            expected_cell = next(m for r in scenario["matrix"] if r["pair_id"] == pair_id for m in r["months"] if m["month"] == selected_month)
            expect(cell(pair_id, selected_month).locator(".half").nth(0)).to_have_class("half " + expected_cell["x"]["band"])
            expect(cell(pair_id, selected_month).locator(".half").nth(1)).to_have_class("half " + expected_cell["y"]["band"])
            individual = next(r for r in scenario["recommendations"] if r["impact_scope"] == "MENTOR" and (r["additional_delta"]["x"] or 0) > 0)
            individual_box = page.locator(f'input[data-action="{individual["action_id"]}"]')
            individual_box.check()
            page.wait_for_function('JSON.parse(sessionStorage.getItem("pulse-pair-session")).selected.length===2')
            ready()
            expect(individual_box).to_be_checked()
            individual_result = client.post(base + "/api/v2/analytics", json={"scope": session()["scope"], "selected": session()["selected"]}).json()
            assert individual_result["indices"]["x"] < scenario["indices"]["x"]
            assert individual_result["indices"]["y"] == scenario["indices"]["y"]
            individual_box.uncheck()
            page.wait_for_function('JSON.parse(sessionStorage.getItem("pulse-pair-session")).selected.length===1')
            ready()
            expect(individual_box).not_to_be_checked()
            assert len(session()["selected"]) == 1
            screenshot("pair-scenario.png")
            page.locator("#compare").click()
            expect(page.locator('#metrics .metric').first).to_contain_text("Баллы из 100")
            assert len(session()["selected"]) == 1
            page.locator("#compare").click()
            expect(page.locator('#metrics .metric').first).to_contain_text("После улучшений")
            checks.append("factual preview and return preserve scenario selections")
            mode("GLOBAL")
            assert len(session()["selected"]) == 1
            mode("PAIR_MONTH")
            click_cell(pair_id, selected_month)
            expect(page.locator(f'input[data-action="{action_id}"]')).to_be_checked()
            page.locator("#manage").click()
            page.get_by_role("textbox", name="Название сценария").fill("Browser saved")
            page.get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.locator("#dialog-body")).to_contain_text("Browser saved")
            page.get_by_role("button", name="Сбросить весь сценарий").click()
            expect(page.locator("#dialog")).not_to_be_visible()
            ready()
            expect(page.locator('#metrics .metric').first).to_contain_text("Баллы из 100")
            assert not session()["selected"]
            page.locator("#manage").click()
            saved_row = page.locator("#dialog .saved .actions").filter(has=page.locator("span", has_text="Browser saved")).first
            saved_row.get_by_role("button", name="Восстановить").click()
            expect(page.locator("#dialog")).not_to_be_visible()
            ready()
            assert len(session()["selected"]) == 1
            page.locator("#manage").click()
            page.get_by_role("button", name="История").first.click()
            expect(page.locator("#dialog-title")).to_contain_text("История")
            expect(page.locator("#dialog table")).to_be_visible()
            page.locator("#close").click()
            checks.extend(["conditional intern action lowers Y only" if expert_baseline else "joint recommendation lowers both indices",
                           "individual recommendation changes only mentor index and can be removed",
                           "scenario colors agree with ML", "selection persists across scopes", "save reset restore and history"])

            # Delay one response after the network finishes. A later scope must win.
            page.evaluate("""() => { const original=window.fetch; window.__originalFetch=original;
                window.fetch=(url,opts)=>{const body=opts?.body?JSON.parse(opts.body):null;
                const p=original(url,opts);return body?.scope?.type==='PAIR_MONTH'&&body.scope.start===1
                    ? p.then(r=>new Promise(resolve=>setTimeout(()=>resolve(r),650))) : p;}; }""")
            cell(pair_id, 1).click()
            cell(pair_id, 2).click()
            ready()
            page.wait_for_timeout(800)
            assert session()["scope"]["start"] == 2
            expect(page.locator("#scope-title")).to_contain_text("М2")
            page.evaluate("window.fetch=window.__originalFetch")
            checks.append("delayed previous HTTP response cannot replace current scope")

            registration = client.post(base + "/api/v2/pairs", json={"intern_id": "synthetic-browser-intern", "mentor_id": "synthetic-browser-mentor", "department": "Синтетическая проверка"})
            registration.raise_for_status()
            synthetic_pair = registration.json()["pair_id"]
            expect(page.locator("#matrix tbody tr")).to_have_count(6)
            assert len(session()["selected"]) == 1
            expect(cell(synthetic_pair, 1).locator(".gray")).to_have_count(2)
            event = {"jsonrpc": "2.0", "method": "submit", "id": 1, "params": {
                "form_id": "synthetic-browser", "response_id": "browser-answer", "revision_id": "1",
                "intern_id": "synthetic-browser-intern", "mentor_id": "synthetic-browser-mentor",
                "department": "Синтетическая проверка", "month": "1", "role": "mentor",
                "clarity": "5", "quality": "5", "autonomy": "5", "deadlines": "5", "initiative": "5", "team": "5",
                "comment": "Синтетический пример", "attention": ""}}
            accepted = client.post(base + "/api/v2/forms/yandex", json=event)
            accepted.raise_for_status()
            assert accepted.json()["result"]["status"] == "accepted"
            assert client.post(base + "/api/v2/forms/yandex", json=event).json()["result"]["status"] == "duplicate"
            expect(cell(synthetic_pair, 1).locator(".half").nth(0)).to_have_class("half green")
            assert len(session()["selected"]) == 1
            checks.append("unrelated pair registration and delivery preserve the selected month scenario")
            expect(cell(synthetic_pair, 1).locator(".half").nth(1)).to_have_class("half gray")
            click_cell(synthetic_pair, 1)
            expect(page.locator('[data-index="z"] b')).to_have_text("—")
            screenshot("pair-missing-side.png")
            checks.extend(["zero-form registry pair appears automatically", "new JSON-RPC response updates correct matrix half", "duplicate webhook remains idempotent", "missing counterpart stays gray and Z unavailable"])

            before_revision = accepted.json()["result"]["source_id"]
            event["params"].update(revision_id="2", clarity="1", quality="1", autonomy="1",
                                   deadlines="1", initiative="1", team="1")
            updated = client.post(base + "/api/v2/forms/yandex", json=event).json()["result"]
            assert updated["source_id"] != before_revision
            expect(cell(synthetic_pair, 1).locator(".half").nth(0)).to_have_class("half red")
            expect(page.locator('[data-index="x"] b')).to_have_text("100")
            expect(page.locator('[data-index="z"] b')).to_have_text("—")
            old = client.get(base + "/api/v2/source/" + before_revision).json()
            assert old["record"]["ratings"]["clarity"] == 5 and len(old["versions"]) == 2
            checks.append("new edition recolors existing half and preserves prior survey and missing partner")
            if expert_baseline:
                page.locator('[data-section="mentor"] input[type="checkbox"]').nth(0).check()
                ready()
                page.locator('[data-section="mentor"] input[type="checkbox"]').nth(1).check()
                ready()
                expect(cell(synthetic_pair, 1).locator(".half").nth(0)).to_have_class("half yellow")
                checks.append("two conditional rating selections recolor mentor red to yellow")

            processes[0].terminate()
            processes[0].wait(timeout=5)
            page.locator('[data-mode="GLOBAL"]').click()
            expect(page.locator('#connection[data-state="error"]')).to_be_visible()
            expect(page.locator("#sections > article")).to_have_count(0)
            expect(page.locator("#message")).to_contain_text("Не удалось получить актуальные индексы")
            screenshot("pair-ml-outage.png")
            assert not errors, errors
            checks.extend(["ML outage clears stale indices and shows retry", "no browser page errors"])
            browser.close()
        report = {"status": "passed", "browser": "headless Microsoft Edge", "historical_data": True,
                  "text_mode": text_mode,
                  "checks": checks, "screenshots": sorted(p.name for p in destination.glob("*.png"))}
        (destination / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mentor", required=True)
    parser.add_argument("--intern", required=True)
    parser.add_argument("--output", default=str(ROOT / "data/review"))
    parser.add_argument("--text-mode", choices=("local_rules","local_encoder"), default="local_rules")
    parser.add_argument("--expert-baseline")
    args = parser.parse_args()
    print(json.dumps(run(args.mentor, args.intern, args.output, args.text_mode, args.expert_baseline), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
