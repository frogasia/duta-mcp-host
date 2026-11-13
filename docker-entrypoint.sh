#!/bin/sh
set -e

# geneco-mcp detached — it owns the only database; restore with it:
# (cd geneco-mcp && alembic upgrade head)

exec uvicorn gateway:app --host 0.0.0.0 --port 8000
