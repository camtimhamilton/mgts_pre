"""Export public OpenAPI and integration examples from two isolated local services."""
import json
from .smoke import ROOT, running_stack


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    destination = ROOT / "integration/examples"
    params = {"form_id": "synthetic-intern", "response_id": "synthetic-answer", "revision_id": "1",
              "intern_id": "demo-I", "department": "Учебное направление", "month": 1, "role": "intern",
              "clarity": 2, "support": 3, "workload": 4, "interest": 4, "growth": 5, "team": 4,
              "satisfaction": 3, "barriers": "Мне не хватает поддержки в учебном проекте."}
    form = {"jsonrpc": "2.0", "method": "submit", "id": "synthetic", "params": params}
    mentor = {"role": "mentor", "month": 1, "intern_id": "demo-I", "mentor_id": "demo-M",
              "department": "Учебное направление",
              "ratings": {"clarity": 2, "quality": 4, "autonomy": 3, "deadlines": 5, "initiative": 4, "team": 5},
              "texts": {"comment": "Работает самостоятельно."}}
    with running_stack(timeout=20, pair_policy="expert") as (client, backend, ml, *_):
        def call(method, url, **kwargs):
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        call("POST", backend+"/api/v2/ingest", json=mentor, headers={"X-Delivery-Id":"synthetic-mentor"})
        call("POST", backend+"/api/v2/forms/yandex", json=form)
        pair = call("GET", backend+"/api/v2/pairs")[0]
        scope = {"type":"PAIR_MONTH","pair_id":pair["pair_id"],"start":1,"end":1}
        actual = call("POST", backend+"/api/v2/analytics", json={"scope":scope})
        recommendation = next(r for r in actual["recommendations"] if (r["delta"]["z"] or 0) > 0)
        scenario = {"scope":scope,"selected":[{"action_id":recommendation["action_id"],"scope":scope}]}
        expected = call("POST", backend+"/api/v2/analytics", json=scenario)
        write(destination / "pair_v2_form.json", form)
        write(destination / "pair_v2_request.json", {"scope":scope})
        write(destination / "pair_v2_analysis.json", actual)
        write(destination / "pair_v2_scenario.json", {"request":scenario,"indices":expected["indices"]})
        write(ROOT / "integration/openapi.json", call("GET", ml+"/openapi.json"))
        write(ROOT / "integration/dashboard_openapi.json", call("GET", backend+"/openapi.json"))
        print("Synthetic example", actual["indices"]["actual"], "=>", expected["indices"]["scenario"])


if __name__ == "__main__":
    main()
