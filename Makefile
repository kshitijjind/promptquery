# Atlas AI reporting bot - convenience targets. ATLAS_DIR points at a checkout of atlas-erp.
ATLAS_DIR ?= ../atlas-erp
COMPOSE = docker compose -p atlas -f $(ATLAS_DIR)/docker-compose.yml -f infra/atlas-compose.override.yml
PY = .venv/bin/python

.PHONY: install atlas-up atlas-seed atlas-down setup-db atlas-migrate test eval eval-holdout chat serve

install:            ## create the venv and install the bot
	uv venv -p 3.12 .venv && uv pip install -p $(PY) -e '.[dev,openai]'

atlas-up:           ## start Atlas Postgres (host port 55433) and backend API (host port 18000)
	$(COMPOSE) up -d --build db backend

atlas-seed:         ## load Atlas demo data (7 tenants), the high-volume tenant, and edge cases
	$(COMPOSE) run --rm seed
	$(COMPOSE) run --rm seed python seed.py --volume
	$(PY) infra/enrich_demo_data.py

atlas-down:         ## stop Atlas and delete its data volume
	$(COMPOSE) down -v

setup-db:           ## create the read-only role + tenant-scoped rpt views, build the schema cache
	.venv/bin/atlas-bot setup-db && .venv/bin/atlas-bot build-catalog

atlas-migrate:      ## migrate Atlas safely: drop rpt views, run migrations, recreate views
	.venv/bin/atlas-bot drop-db-views --yes
	cd $(ATLAS_DIR)/backend && uv run alembic upgrade head
	$(MAKE) setup-db

test:               ## unit + integration tests (integration tests skip if the DB is down)
	$(PY) -m pytest -q

eval:               ## execution-accuracy eval on the dev sets (single, multi-turn, security)
	$(PY) eval/atlas_eval.py --workers 4

eval-holdout:       ## eval on the held-out set
	$(PY) eval/atlas_eval.py --holdout --workers 4

chat:               ## interactive multi-turn chat in the terminal
	.venv/bin/atlas-bot chat

serve:              ## HTTP API + web chat on http://127.0.0.1:8088
	.venv/bin/atlas-bot serve
