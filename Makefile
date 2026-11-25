.PHONY: up down logs dev test seed recompute clean-kit init clean

up:
	@test -f .env || cp .env.example .env
	mkdir -p data
	docker compose up --build -d
	@echo "дашборд: http://localhost:8000"

seed:
	docker compose exec pulse python -m tools.seed_demo --seed 42 --reset

recompute:
	docker compose exec pulse python -m tools.recompute

clean-kit:
	docker compose exec pulse python -m tools.clean_form_kit

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

dev:
	@test -f .env || cp .env.example .env
	uvicorn app.main:app --reload --port 8000

test:
	python -m pytest -q

init:
	python -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

clean:
	rm -rf data .pytest_cache __pycache__ app/__pycache__ tests/__pycache__
