.PHONY: install run docker-up docker-down

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ./shared
	# geneco-mcp detached — restore with `-e ./geneco-mcp`
	.venv/bin/pip install -e ./ytllead-mcp "fastapi>=0.115.0" "uvicorn[standard]>=0.34.0"

run:
	# settings read .env relative to the cwd, so load ytllead-mcp/.env explicitly
	set -a; [ -f ytllead-mcp/.env ] && . ytllead-mcp/.env; set +a; \
	.venv/bin/uvicorn gateway:app --host 0.0.0.0 --port 8000 --reload

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down
