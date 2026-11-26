"""Optional real headless Edge UI smoke, on synthetic data and local services."""
import json
import re

from playwright.sync_api import expect, sync_playwright

from .smoke import ROOT, exercise, running_stack


def main():
    with running_stack() as stack:
        client, base, ml_base, temp, processes = stack
        exercise(*stack, outage=False)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/legacy")
            expect(page.locator(".hero-risk")).to_have_text("17")
            expect(page.locator("#card .reason")).to_have_count(10)
            expect(page.locator(".ml-status")).to_contain_text("22%")
            expect(page.locator("#card")).to_contain_text("не вероятность увольнения")
            pair_id = client.get(base + "/api/pairs?month=M3").json()["items"][0]["id"]
            card = client.get(base + f"/api/pairs/{pair_id}?month=M3").json()
            action_index = next(i for i, step in enumerate(card["steps"]) if step["key"] == "A03")
            page.locator(".ml-step input").nth(action_index).uncheck()
            page.get_by_role("button", name="Рассчитать выбранные мероприятия").click()
            expect(page.locator("#card")).to_contain_text("условный 5 баллов")
            expect(page.locator(".hero-risk")).to_have_text("17")
            output = ROOT / "integration/screenshots"
            output.mkdir(exist_ok=True)
            page.locator("#card").evaluate("element => element.scrollTop = 0")
            page.screenshot(path=output / "ml-dashboard.png", full_page=True)
            page.select_option("#month", "M1")
            page.locator("#list .row-person").first.click()
            expect(page.locator(".hero-risk")).to_have_text("0")
            # Heatmap exposes missing month even if it has no submitted forms.
            page.select_option("#month", "M2")
            page.locator("#list .row-person").first.click()
            expect(page.locator(".hero-risk")).to_have_text("—")
            expect(page.locator(".ml-status")).to_contain_text("нет анкет")
            # Delayed previous-month response must never overwrite the active card.
            page.evaluate("""pairId => {
              window.__savedFetch = window.fetch;
              window.fetch = (url, ...args) => url.includes(`/api/pairs/${pairId}?month=M3`)
                ? new Promise(resolve => setTimeout(() => window.__savedFetch(url, ...args).then(resolve), 350))
                : window.__savedFetch(url, ...args);
            }""", pair_id)
            page.select_option("#month", "M3")
            page.locator("#list .row-person").first.click()
            page.select_option("#month", "M2")
            page.locator("#list .row-person").first.click()
            expect(page.locator(".hero-risk")).to_have_text("—")
            page.wait_for_timeout(450)
            expect(page.locator(".hero-risk")).to_have_text("—")
            page.evaluate("window.fetch = window.__savedFetch")
            assert not errors, errors
            browser.close()
    print(json.dumps({"status": "passed", "browser": "headless Microsoft Edge", "synthetic": True,
        "checks": ["10 dynamic factors", "coverage 22%", "scenario 5 / fact 17", "M1 score 0", "M2 missing", "stale month response rejected", "no page errors"]}, indent=2))


if __name__ == "__main__":
    main()
