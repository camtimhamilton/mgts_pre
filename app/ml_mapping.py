"""Actual dashboard form wording. Numeric policy lives only in ML YAML."""
from .config import get_settings

QUESTIONS = {
    "intern": {
        "motivation": "Насколько тебе сейчас интересно то, чем ты занимаешься?",
        "clarity": "Насколько понятно, какие у тебя задачи и чего от тебя ждут?",
        "mentor": "Насколько тебе хватает поддержки и обратной связи от наставника?",
        "workload": "Насколько нагрузка сейчас комфортна?",
        "inclusion": "Насколько ты чувствуешь себя частью команды?",
        "future": "Насколько ты уверен, что хочешь и сможешь остаться в компании после стажировки?",
        "q_better": "Что за последний месяц изменилось к лучшему?",
        "q_barrier": "Что мешает работать или снижает желание продолжать?",
    },
    "mentor": {
        "motivation": "Насколько стажёр вовлечён в работу и проявляет инициативу?",
        "clarity": "Насколько стажёр понимает свои задачи и ожидания?",
        "mentor": "Насколько вам комфортно работать вместе и хватает ли времени на поддержку?",
        "workload": "Насколько нагрузка стажёра адекватна?",
        "inclusion": "Насколько стажёр включён в жизнь команды?",
        "future": "Насколько вы видите потенциал стажёра остаться в компании?",
        "q_better": "Как изменилась самостоятельность стажёра за этот месяц?",
        "q_barrier": "Что, на ваш взгляд, требует вмешательства?",
    },
}


def survey_payload(pair_id: int, month: str, submissions: list[dict]) -> dict:
    settings = get_settings()
    responses = []
    for item in sorted(submissions, key=lambda s: s["role"]):
        role = item["role"]
        answers = [{"type": "rating", "question_id": "f_" + key,
                    "question_text": QUESTIONS[role][key], "value": value,
                    "scale_min": 1, "scale_max": 10} for key, value in sorted(item.get("scores", {}).items())]
        for key in ("q_better", "q_barrier"):
            if item.get(key):
                answers.append({"type": "text", "question_id": key,
                                "question_text": QUESTIONS[role][key], "value": item[key]})
        responses.append({"response_id": f"pair_{pair_id}_{month}_{role}", "respondent_role": role, "answers": answers})
    return {"program_id": settings.ml_program_id, "intern_id": f"pair_{pair_id}",
            "period": int(month[1:]), "mapping_version": settings.ml_mapping_version,
            "knowledge_version": settings.ml_knowledge_version, "responses": responses}
