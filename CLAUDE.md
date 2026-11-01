# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Expense Tracker

## Product context
- A conversational AI agent where users log daily expenses in plain English and get spending breakdowns by category
- Personal use: someone tracking monthly spending without a spreadsheet, across fixed and variable expenses
- Done for this phase: user can add/edit/delete expenses, view summaries (this week, this month, custom range), get recurring expenses auto-filled, and export to CSV

## Tech stack
- Framework: Google ADK (`google-adk`) with `LlmAgent`
- Model: Gemini 2.0 Flash via Gemini API (or Vertex AI)
- Storage: SQLite via Python's built-in `sqlite3` (two tables: `expenses`, `recurring_expenses`)
- Dependency manager: `uv`
- Python: 3.10+

## Commands
- Dev: `uv run adk web` (browser chat UI) or `uv run adk run expense_tracker` (terminal)
- Test: `uv run pytest` | single test: `uv run pytest tests/test_tools.py::test_add_expense`
- Lint: `uv run ruff check . && uv run ruff format --check .`

## Where things live
- Agent entry point: `expense_tracker/agent.py` — root `LlmAgent` definition
- System prompt: `expense_tracker/prompt.py`
- Tool functions: `expense_tracker/tools.py` — all SQLite reads/writes live here
- Category keyword map: `expense_tracker/categories.py` — smart matching rules
- Database: `expenses.db` (created at runtime, path overridable via `EXPENSE_DB_PATH`)
- Exports: `exports/` directory (created at runtime), CSV files written here
- Env config: `.env` (copy from `.env.example`)

## Tools (`tools.py`)
| Function | Purpose |
|---|---|
| `add_expense(amount, category, description, expense_date?, currency?)` | Insert a row |
| `edit_expense(expense_id, amount?, category?, description?, expense_date?)` | Update any field on an existing expense |
| `delete_expense(expense_id)` | Remove a row by ID |
| `list_expenses(start_date?, end_date?, category?)` | Query rows with filters |
| `get_summary(period)` | "this week", "this month", "last month", or "YYYY-MM-DD to YYYY-MM-DD" |
| `get_monthly_summary(year, month)` | Category-wise totals + top category for a specific month |
| `set_recurring_expense(category, amount, description, day_of_month?, currency?)` | Save a recurring expense template |
| `list_recurring_expenses()` | Show all active recurring templates |
| `apply_recurring_expenses(year, month)` | Auto-insert recurring rows for the month; returns what was added |
| `export_expenses(start_date, end_date)` | Write a CSV to `exports/` and return the file path |

## DB schema
**`expenses`**: `id`, `amount` (REAL), `currency` (TEXT, default `EUR`), `category` (TEXT), `description` (TEXT), `date` (TEXT, YYYY-MM-DD)

**`recurring_expenses`**: `id`, `category` (TEXT), `amount` (REAL), `currency` (TEXT, default `EUR`), `description` (TEXT), `day_of_month` (INTEGER, default 1), `active` (INTEGER, default 1)

## Valid categories
`Groceries`, `Transport`, `Entertainment`, `Shopping`, `Dining Out`, `Rent/Mortgage`, `City Tax`, `Mobile Recharge`, `Beauty & Health`, `Trips`, `Miscellaneous Expenses`, `Home Insurance`, `Home Budget`, `Water`, `Internet`, `Gas & Electricity`

## Smart category matching (`categories.py`)
Keywords map to categories — agent uses this before asking the user. Examples:
- "coffee", "lunch", "restaurant", "takeaway", "bar" → `Dining Out`
- "Uber", "bus", "train", "metro", "fuel", "parking", "taxi" → `Transport`
- "Netflix", "Spotify", "cinema", "concert", "Steam" → `Entertainment`
- "H&M", "Zara", "Amazon", "clothes", "shoes" → `Shopping`
- "gym", "pharmacy", "doctor", "dentist", "haircut" → `Beauty & Health`
- "flight", "hotel", "Airbnb", "holiday" → `Trips`
- "mortgage", "rent" → `Rent/Mortgage`
- "electric", "gas", "electricity" → `Gas & Electricity`

## Recurring expenses behaviour
- Default recurring set: `Rent/Mortgage`, `Internet`, `Water`, `City Tax`
- `apply_recurring_expenses()` is called on the first conversation of each calendar month
- After auto-inserting, the agent notifies the user: *"I've pre-filled your recurring expenses for [Month] — please check the amounts and let me know if anything needs updating"*
- Users can add, edit, or deactivate recurring templates at any time

## Rules
- Default currency is **EUR (€)**; only override per-row when the user explicitly states a different currency
- All DB writes go through `tools.py` — no inline SQL elsewhere
- Tool docstrings and type hints are the source of truth for ADK-generated JSON schemas — keep them accurate or the agent will mis-call tools
- Valid categories and keyword mappings are canonical in `tools.py:VALID_CATEGORIES` and `categories.py` — never hardcode elsewhere
- `expense_date` always stored and compared as `YYYY-MM-DD` strings — no datetime objects in the DB layer
- CSV exports go to `exports/` — never write files to the project root

## Unclear input handling
1. Agent cannot parse the input → ask user to repeat: *"I didn't catch that — could you say it again? e.g. 'I spent €20 on transport yesterday'"*
2. Still unclear → ask user to type it out explicitly
3. If category is ambiguous, agent suggests the closest match and confirms: *"That sounds like Dining Out — shall I log it there?"*

## Gotchas
- ADK's `adk web` discovers the agent via the `expense_tracker/` package name — the directory name must match the Python package name (underscores, not hyphens)
- `GOOGLE_GENAI_USE_VERTEXAI=1` requires `GOOGLE_CLOUD_PROJECT` and ADC credentials; without it `GOOGLE_API_KEY` is used — mixing the two silently falls back and can appear broken
- SQLite creates `expenses.db` relative to wherever `adk run` is invoked — always run commands from the project root
- `apply_recurring_expenses` must be idempotent — check if rows for that month already exist before inserting to avoid duplicates on reconnect

## Workflow
- Plan first for anything touching more than one of `agent.py`, `prompt.py`, `tools.py`, `categories.py` simultaneously
- Run `uv run pytest` before declaring any change done
