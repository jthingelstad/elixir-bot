#!/bin/bash
cd /Users/otto/Projects/elixir-bot-v2
export ELIXIR_DB_PATH=/Users/otto/Projects/elixir-bot-v2/elixir-v51.db
set -a; source /Users/otto/Projects/elixir-bot-v2/.env 2>/dev/null; set +a
/Users/otto/Projects/elixir-bot/.venv/bin/python scripts/enrich_card_facts.py \
  --db "$ELIXIR_DB_PATH" > /Users/otto/Projects/elixir-bot-v2/enrich.log 2>&1
echo "EXIT=$?" >> /Users/otto/Projects/elixir-bot-v2/enrich.log
