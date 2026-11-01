"""SQLite- and PostgreSQL-backed tool functions for the expense tracker agent."""

from __future__ import annotations

import csv
import io
import os
import urllib.parse
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from .categories import VALID_CATEGORIES, match_category

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH      = os.environ.get("EXPENSE_DB_PATH", "expenses.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
EXPORTS_DIR  = os.environ.get("EXPENSE_EXPORTS_DIR", "exports")
DEFAULT_CURRENCY = "EUR"
_USE_PG = bool(DATABASE_URL)


# ---------------------------------------------------------------------------
# DB bootstrap & helpers
# ---------------------------------------------------------------------------

def _get_connection():
    """Return a DB connection with both tables guaranteed to exist."""
    if _USE_PG:
        import psycopg2, psycopg2.extras
        conn = psycopg2.connect(os.environ["DATABASE_URL"],
                                cursor_factory=psycopg2.extras.RealDictCursor)
        _bootstrap_pg(conn)
        return conn
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EUR', category TEXT NOT NULL,
            description TEXT NOT NULL, date TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recurring_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
            amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR',
            description TEXT NOT NULL, day_of_month INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1);
    """)
    conn.commit()
    return conn


def _bootstrap_pg(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY, amount NUMERIC(12,2) NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EUR', category TEXT NOT NULL,
            description TEXT NOT NULL, date TEXT NOT NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS recurring_expenses (
            id SERIAL PRIMARY KEY, category TEXT NOT NULL,
            amount NUMERIC(12,2) NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR',
            description TEXT NOT NULL, day_of_month INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1)""")
    conn.commit()


def _clean(row: dict) -> dict:
    """Convert Decimal values (PostgreSQL NUMERIC) to float for JSON serialisation."""
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


def _fetchall(conn, sql: str, params=None) -> list[dict]:
    if _USE_PG:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or ())
            return [_clean(dict(r)) for r in cur.fetchall()]
    return [dict(r) for r in conn.execute(sql, params or ()).fetchall()]


def _fetchone(conn, sql: str, params=None) -> dict | None:
    if _USE_PG:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or ())
            row = cur.fetchone()
            return _clean(dict(row)) if row else None
    row = conn.execute(sql, params or ()).fetchone()
    return dict(row) if row else None


def _insert(conn, sql: str, params=None) -> int:
    """Execute INSERT and return the new row id."""
    if _USE_PG:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s") + " RETURNING id", params or ())
            return cur.fetchone()["id"]
    cursor = conn.execute(sql, params or ())
    return cursor.lastrowid


def _run(conn, sql: str, params=None) -> None:
    """Execute UPDATE or DELETE."""
    if _USE_PG:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or ())
    else:
        conn.execute(sql, params or ())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_category(category: str) -> str | None:
    """Return the canonical category string, or None if invalid."""
    matched = next(
        (c for c in VALID_CATEGORIES if c.lower() == category.strip().lower()),
        None,
    )
    return matched


def _format_currency(amount: float, currency: str) -> str:
    symbol = "€" if currency.upper() == "EUR" else currency.upper()
    return f"{symbol}{amount:,.2f}"


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return (first_day, last_day) as ISO strings for a given month."""
    first = date(year, month, 1)
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


# ---------------------------------------------------------------------------
# Expense CRUD
# ---------------------------------------------------------------------------

def add_expense(
    amount: float,
    description: str,
    category: Optional[str] = None,
    expense_date: Optional[str] = None,
    currency: Optional[str] = None,
) -> dict:
    """Add a new expense record.

    If category is not provided, it is inferred automatically from the
    description using keyword matching. Unrecognised descriptions default
    to "Miscellaneous Expenses".

    Args:
        amount: Expense amount as a positive number.
        description: Brief description of what the expense was for
            (e.g. "Uber to the airport", "Albert Heijn groceries").
        category: Optional. One of the valid categories. Auto-detected from
            description when omitted.
        expense_date: Optional. Date in YYYY-MM-DD format. Defaults to today.
        currency: Optional. Three-letter currency code (e.g. EUR, USD, GBP).
            Defaults to EUR.

    Returns:
        A dict with the saved expense details and a confirmation message.
    """
    if amount <= 0:
        return {"error": "Amount must be a positive number."}

    # Resolve category
    if category:
        resolved = _normalise_category(category)
        if not resolved:
            return {
                "error": (
                    f"'{category}' is not a valid category. "
                    f"Valid options: {', '.join(VALID_CATEGORIES)}"
                )
            }
        category = resolved
    else:
        category = match_category(description)

    # Resolve date
    if expense_date is None:
        expense_date = date.today().isoformat()
    else:
        try:
            datetime.strptime(expense_date, "%Y-%m-%d")
        except ValueError:
            return {"error": "expense_date must be in YYYY-MM-DD format."}

    currency = (currency or DEFAULT_CURRENCY).upper()

    conn = _get_connection()
    expense_id = _insert(
        conn,
        "INSERT INTO expenses (amount, currency, category, description, date) VALUES (?, ?, ?, ?, ?)",
        (amount, currency, category, description.strip(), expense_date),
    )
    conn.commit()
    conn.close()

    return {
        "id": expense_id,
        "amount": amount,
        "currency": currency,
        "category": category,
        "description": description.strip(),
        "date": expense_date,
        "message": (
            f"Added expense #{expense_id}: {category} — {description} "
            f"for {_format_currency(amount, currency)} on {expense_date}."
        ),
    }


def edit_expense(
    expense_id: int,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    description: Optional[str] = None,
    expense_date: Optional[str] = None,
    currency: Optional[str] = None,
) -> dict:
    """Edit one or more fields on an existing expense.

    Only the fields you pass will be updated; everything else stays the same.

    Args:
        expense_id: The numeric ID of the expense to edit.
        amount: New amount (positive number).
        category: New category (must be a valid category name).
        description: New description text.
        expense_date: New date in YYYY-MM-DD format.
        currency: New currency code (e.g. EUR, USD).

    Returns:
        A dict with the updated expense details and a confirmation message.
    """
    conn = _get_connection()
    current = _fetchone(conn, "SELECT * FROM expenses WHERE id = ?", (expense_id,))
    if current is None:
        conn.close()
        return {"error": f"No expense found with id {expense_id}."}

    # Validate and apply updates
    if amount is not None:
        if amount <= 0:
            conn.close()
            return {"error": "Amount must be a positive number."}
        current["amount"] = amount

    if category is not None:
        resolved = _normalise_category(category)
        if not resolved:
            conn.close()
            return {
                "error": (
                    f"'{category}' is not a valid category. "
                    f"Valid options: {', '.join(VALID_CATEGORIES)}"
                )
            }
        current["category"] = resolved

    if description is not None:
        current["description"] = description.strip()

    if expense_date is not None:
        try:
            datetime.strptime(expense_date, "%Y-%m-%d")
        except ValueError:
            conn.close()
            return {"error": "expense_date must be in YYYY-MM-DD format."}
        current["date"] = expense_date

    if currency is not None:
        current["currency"] = currency.upper()

    _run(
        conn,
        """UPDATE expenses
           SET amount = ?, currency = ?, category = ?, description = ?, date = ?
           WHERE id = ?""",
        (
            current["amount"],
            current["currency"],
            current["category"],
            current["description"],
            current["date"],
            expense_id,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "id": expense_id,
        "amount": current["amount"],
        "currency": current["currency"],
        "category": current["category"],
        "description": current["description"],
        "date": current["date"],
        "message": (
            f"Expense #{expense_id} updated: {current['category']} — "
            f"{current['description']} for "
            f"{_format_currency(current['amount'], current['currency'])} "
            f"on {current['date']}."
        ),
    }


def delete_expense(expense_id: int) -> dict:
    """Delete an expense by its ID.

    Args:
        expense_id: The numeric ID of the expense to delete.

    Returns:
        A confirmation message, or an error if the ID was not found.
    """
    conn = _get_connection()
    expense = _fetchone(conn, "SELECT * FROM expenses WHERE id = ?", (expense_id,))
    if expense is None:
        conn.close()
        return {"error": f"No expense found with id {expense_id}."}

    _run(conn, "DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()

    return {
        "message": (
            f"Deleted expense #{expense_id}: {expense['category']} — "
            f"{expense['description']} "
            f"({_format_currency(expense['amount'], expense['currency'])} "
            f"on {expense['date']})."
        )
    }


# ---------------------------------------------------------------------------
# Queries & summaries
# ---------------------------------------------------------------------------

def list_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """List expenses filtered by date range and/or category.

    Args:
        start_date: Start of range in YYYY-MM-DD format (inclusive).
            Defaults to the first day of the current month.
        end_date: End of range in YYYY-MM-DD format (inclusive).
            Defaults to today.
        category: Optional category filter (e.g. "Groceries").

    Returns:
        A dict with a list of expenses, count, and total amount.
    """
    today = date.today()
    if start_date is None:
        start_date = today.replace(day=1).isoformat()
    if end_date is None:
        end_date = today.isoformat()

    query = "SELECT * FROM expenses WHERE date BETWEEN ? AND ?"
    params: list = [start_date, end_date]

    if category:
        resolved = _normalise_category(category)
        if resolved:
            query += " AND category = ?"
            params.append(resolved)

    query += " ORDER BY date DESC, id DESC"

    conn = _get_connection()
    expenses = _fetchall(conn, query, params)
    conn.close()

    total = round(sum(e["amount"] for e in expenses), 2)

    return {
        "expenses": expenses,
        "count": len(expenses),
        "total": total,
        "period": f"{start_date} to {end_date}",
    }


def get_summary(period: str) -> dict:
    """Get a spending summary for a named or custom period.

    Supported period values:
    - "this week"  — Monday through today
    - "this month" — 1st of current month through today
    - "last month" — full previous calendar month
    - A month name, e.g. "October" or "October 2025"
    - A custom range, e.g. "2025-10-01 to 2025-10-15"

    Args:
        period: A period descriptor string (see above).

    Returns:
        A dict with per-category totals, grand total, and top category.
    """
    today = date.today()
    period_clean = period.strip().lower()

    if period_clean == "this week":
        start = (today - timedelta(days=today.weekday())).isoformat()
        end = today.isoformat()

    elif period_clean == "this month":
        start = today.replace(day=1).isoformat()
        end = today.isoformat()

    elif period_clean == "last month":
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        start, end = _month_bounds(last_month_end.year, last_month_end.month)

    elif " to " in period_clean:
        parts = period_clean.split(" to ")
        if len(parts) != 2:
            return {"error": "Custom range must be in 'YYYY-MM-DD to YYYY-MM-DD' format."}
        start, end = parts[0].strip(), parts[1].strip()
        for d in (start, end):
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                return {"error": f"Invalid date '{d}'. Use YYYY-MM-DD format."}

    else:
        # Try to parse as a month name, e.g. "october" or "october 2025"
        parsed = None
        for fmt in ("%B %Y", "%B"):
            try:
                parsed = datetime.strptime(period_clean.title(), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return {
                "error": (
                    f"Could not understand period '{period}'. "
                    "Try: 'this week', 'this month', 'last month', "
                    "'October', 'October 2025', or '2025-10-01 to 2025-10-15'."
                )
            }
        year = parsed.year if parsed.year != 1900 else today.year
        start, end = _month_bounds(year, parsed.month)

    return _build_summary(start, end, label=period)


def get_monthly_summary(year: int, month: int) -> dict:
    """Get a category-wise spending breakdown for a specific month.

    Args:
        year: Four-digit year (e.g. 2025).
        month: Month number 1–12.

    Returns:
        A dict with per-category totals, grand total, and top category.
    """
    if not (1 <= month <= 12):
        return {"error": "month must be between 1 and 12."}

    start, end = _month_bounds(year, month)
    label = datetime(year, month, 1).strftime("%B %Y")
    return _build_summary(start, end, label=label)


def _build_summary(start: str, end: str, label: str) -> dict:
    """Shared summary builder used by get_summary and get_monthly_summary."""
    conn = _get_connection()
    breakdown = _fetchall(
        conn,
        """SELECT category,
                  ROUND(SUM(amount), 2) AS total,
                  COUNT(*)              AS count
           FROM expenses
           WHERE date BETWEEN ? AND ?
           GROUP BY category
           ORDER BY total DESC""",
        (start, end),
    )
    conn.close()

    grand_total = round(sum(r["total"] for r in breakdown), 2)
    top_category = breakdown[0]["category"] if breakdown else None

    # Build a WhatsApp-ready text summary
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 {label} — €{grand_total:,.2f} total", ""]
    for i, row in enumerate(breakdown):
        prefix = medals[i] if i < 3 else "   "
        lines.append(f"{prefix} {row['category']}  €{row['total']:,.2f}")
    if top_category:
        lines += ["", f"Top spend: {top_category}"]

    whatsapp_text = "\n".join(lines)
    whatsapp_url = "https://wa.me/?text=" + urllib.parse.quote(whatsapp_text)

    return {
        "period": f"{start} to {end}",
        "label": label,
        "breakdown": breakdown,
        "grand_total": grand_total,
        "top_category": top_category,
        "whatsapp_url": whatsapp_url,
        "message": (
            f"{label}: €{grand_total:,.2f} total. "
            + (f"Top category: {top_category}." if top_category else "No expenses recorded.")
        ),
    }


# ---------------------------------------------------------------------------
# Recurring expenses
# ---------------------------------------------------------------------------

def set_recurring_expense(
    category: str,
    amount: float,
    description: str,
    day_of_month: Optional[int] = 1,
    currency: Optional[str] = None,
) -> dict:
    """Save or update a recurring monthly expense template.

    If a recurring entry with the same category and description already exists,
    it is updated. Otherwise a new template is created.

    Args:
        category: The expense category (must be a valid category).
        amount: Monthly amount as a positive number.
        description: Label for this recurring expense (e.g. "Rent", "KPN Internet").
        day_of_month: Day of the month on which the expense recurs (1–28). Defaults to 1.
        currency: Currency code. Defaults to EUR.

    Returns:
        A dict with the saved template and a confirmation message.
    """
    resolved = _normalise_category(category)
    if not resolved:
        return {
            "error": (
                f"'{category}' is not a valid category. "
                f"Valid options: {', '.join(VALID_CATEGORIES)}"
            )
        }

    if amount <= 0:
        return {"error": "Amount must be a positive number."}

    day_of_month = day_of_month or 1
    if not (1 <= day_of_month <= 28):
        return {"error": "day_of_month must be between 1 and 28."}

    currency = (currency or DEFAULT_CURRENCY).upper()

    conn = _get_connection()
    existing = _fetchone(
        conn,
        "SELECT id FROM recurring_expenses WHERE category = ? AND description = ?",
        (resolved, description.strip()),
    )

    if existing:
        _run(
            conn,
            """UPDATE recurring_expenses
               SET amount = ?, currency = ?, day_of_month = ?, active = 1
               WHERE id = ?""",
            (amount, currency, day_of_month, existing["id"]),
        )
        action = "Updated"
        record_id = existing["id"]
    else:
        record_id = _insert(
            conn,
            """INSERT INTO recurring_expenses
               (category, amount, currency, description, day_of_month, active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (resolved, amount, currency, description.strip(), day_of_month),
        )
        action = "Created"

    conn.commit()
    conn.close()

    return {
        "id": record_id,
        "category": resolved,
        "amount": amount,
        "currency": currency,
        "description": description.strip(),
        "day_of_month": day_of_month,
        "message": (
            f"{action} recurring expense #{record_id}: {resolved} — "
            f"{description} for {_format_currency(amount, currency)} "
            f"on day {day_of_month} of every month."
        ),
    }


def list_recurring_expenses() -> dict:
    """List all active recurring expense templates.

    Returns:
        A dict with the list of recurring templates and their details.
    """
    conn = _get_connection()
    templates = _fetchall(
        conn,
        "SELECT * FROM recurring_expenses WHERE active = 1 ORDER BY category, description",
    )
    conn.close()

    return {
        "recurring_expenses": templates,
        "count": len(templates),
        "message": (
            f"{len(templates)} active recurring expense(s)."
            if templates
            else "No recurring expenses set up yet."
        ),
    }


def apply_recurring_expenses(year: int, month: int) -> dict:
    """Auto-insert recurring expenses for a given month (idempotent).

    Safe to call multiple times — skips any recurring expense that has
    already been inserted for the given month (matched by category,
    description, and date).

    Args:
        year: Four-digit year.
        month: Month number 1–12.

    Returns:
        A dict listing what was added and what was skipped.
    """
    if not (1 <= month <= 12):
        return {"error": "month must be between 1 and 12."}

    conn = _get_connection()
    templates = _fetchall(conn, "SELECT * FROM recurring_expenses WHERE active = 1")

    added = []
    skipped = []

    for t in templates:
        # Clamp day to last day of month (handles Feb, 30-day months)
        try:
            expense_date = date(year, month, t["day_of_month"]).isoformat()
        except ValueError:
            last_day = (date(year, month + 1, 1) - timedelta(days=1)).day if month < 12 else 31
            expense_date = date(year, month, last_day).isoformat()

        # Idempotency check
        exists = _fetchone(
            conn,
            "SELECT id FROM expenses WHERE category = ? AND description = ? AND date = ?",
            (t["category"], t["description"], expense_date),
        )

        if exists:
            skipped.append(t["description"])
            continue

        _insert(
            conn,
            "INSERT INTO expenses (amount, currency, category, description, date) VALUES (?, ?, ?, ?, ?)",
            (t["amount"], t["currency"], t["category"], t["description"], expense_date),
        )
        added.append({
            "category": t["category"],
            "description": t["description"],
            "amount": t["amount"],
            "currency": t["currency"],
            "date": expense_date,
        })

    conn.commit()
    conn.close()

    month_label = datetime(year, month, 1).strftime("%B %Y")
    return {
        "month": month_label,
        "added": added,
        "skipped": skipped,
        "message": (
            f"Pre-filled {len(added)} recurring expense(s) for {month_label}. "
            + (f"Skipped {len(skipped)} already present. " if skipped else "")
            + "Please check the amounts and let me know if anything needs updating."
            if added else
            f"All recurring expenses for {month_label} were already present."
        ),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Export expenses as CSV content for a given date range.

    The CSV has 6 columns: id, date, amount, currency, category, description.
    The content is returned as a string so the agent can display it directly
    in chat.

    Args:
        start_date: Start of range in YYYY-MM-DD format (inclusive).
            Defaults to the first day of the current month.
        end_date: End of range in YYYY-MM-DD format (inclusive).
            Defaults to today.

    Returns:
        A dict with csv_content, row_count, period, and a message.
    """
    today = date.today()
    if start_date is None:
        start_date = today.replace(day=1).isoformat()
    if end_date is None:
        end_date = today.isoformat()

    conn = _get_connection()
    rows = _fetchall(
        conn,
        """SELECT id, date, amount, currency, category, description
           FROM expenses
           WHERE date BETWEEN ? AND ?
           ORDER BY date ASC, id ASC""",
        (start_date, end_date),
    )
    conn.close()

    if not rows:
        return {
            "csv_content": "",
            "row_count": 0,
            "period": f"{start_date} to {end_date}",
            "message": f"No expenses found between {start_date} and {end_date}.",
        }

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["id", "date", "amount", "currency", "category", "description"],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    csv_content = buf.getvalue()

    period = f"{start_date} to {end_date}"
    return {
        "csv_content": csv_content,
        "row_count": len(rows),
        "period": period,
        "message": (
            f"Here are your expenses for {period} ({len(rows)} rows). "
            "Copy the CSV content below."
        ),
    }
