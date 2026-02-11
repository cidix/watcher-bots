# watcher-bots

A collection of small, independent watcher bots.

Each bot monitors external signals (prices, availability, changes) and sends alerts when predefined conditions are met.

Bots live under `bots/<bot-id>/` and run as scheduled GitHub Actions.

## Bots

- **budget** – personal budgeting tracker / notifier (Telegram)  
  Workflow: `.github/workflows/budget.yml`

- **garmin** – Garmin Forerunner 965 price watcher (Telegram)  
  Workflow: `.github/workflows/garmin_price_watcher.yml`

- **shop-sale-watcher** – shop sale / deal watcher (Telegram)  
  Workflow: `.github/workflows/shop-sale-watcher.yml`

## Secrets & environment

Standard (used by all workflows):
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Budget bot compatibility:
- The budget bot prefers the standard `TELEGRAM_*` variables and falls back to `BUDGET_TELEGRAM_*`.
- State directory: prefers `DATA_DIR`, falls back to `BUDGET_DATA_DIR`, else defaults to `bots/budget/data`.

## State persistence

Bots persist state under `bots/<bot-id>/data/` and GitHub Actions commits that state back to the repository.
This enables idempotent watchers (no duplicate alerts) across runs.

## How to add a new bot

1. Create `bots/<bot-id>/` and store runtime state in `bots/<bot-id>/data/`.
2. Add a workflow under `.github/workflows/<bot-id>.yml` with a schedule and optional `workflow_dispatch`.
3. In the workflow, call the composite action `./.github/actions/run-watcher` with:
   - `bot_id: <bot-id>`
   - `run_cmd: python bots/<bot-id>/...`
4. Provide `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` via GitHub Secrets.
