.PHONY: up down seed test test-docker migrate lint demo dev-backend dev-frontend

up:            ## start everything (postgres + backend + frontend)
	docker compose up --build

down:
	docker compose down

seed:          ## seed Demo Store, Kigali Fashion, Mama's Electronics
	docker compose exec backend python -m seed.seed

migrate:
	docker compose exec backend alembic upgrade head

test-docker:   ## run the test suite inside the backend container (uses a separate test DB)
	docker compose exec db psql -U commerce -c "CREATE DATABASE commerce_test" || true
	docker compose exec -e TEST_DATABASE_URL=postgresql+psycopg://commerce:commerce@db:5432/commerce_test backend pytest -q

# --- without docker (local postgres with pgvector on :5432) ---
dev-backend:
	cd backend && alembic upgrade head && uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm install && npm run dev

seed-local:
	cd backend && python -m seed.seed

test:
	cd backend && pytest -q

lint:
	cd backend && ruff check app tests seed

demo:          ## replay the section-37 demo conversation through the WhatsApp simulator
	scripts/demo_chat.sh fashion@duka.dev 250788555666 "Hi, I'm looking for black sneakers under 100,000 RWF." \
	  "Add the second one." "How much including delivery?" "Place the order." "Pay."
