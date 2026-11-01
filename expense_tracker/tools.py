"""SQLite-backed tool functions for the expense tracker agent."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Optional

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from .categories import VALID_CATEGORIES, match_category

DB_PATH = os.environ.get("EXPENSE_DB_PATH", "expenses.db")
EXPORTS_DIR = os.environ.get("EXPENSE_EXPORTS_DIR", "exports")

DEFAULT_CURRENCY = "EUR"


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    """Return a connection with both tables guaranteed to exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            amount      REAL    NOT NULL,
            currency    TEXT    NOT NULL DEFAULT 'EUR',
            category    TEXT    NOT NULL,
            description TEXT    NOT NULL,
            date        TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recurring_expenses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            category     TEXT    NOT NULL,
            amount       REAL    NOT NULL,
            currency     TEXT    NOT NULL DEFAULT 'EUR',
            description  TEXT    NOT NULL,
            day_of_month INTEGER NOT NULL DEFAULT 1,
            active       INTEGER NOT NULL DEFAULT 1
        );
    """)
    conn.commit()
    return conn


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
    cursor = conn.execute(
        """INSERT INTO expenses (amount, currency, category, description, date)
           VALUES (?, ?, ?, ?, ?)""",
        (amount, currency, category, description.strip(), expense_date),
    )
    conn.commit()
    expense_id = cursor.lastrowid
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
    row = conn.execute(
        "SELECT * FROM expenses WHERE id = ?", (expense_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return {"error": f"No expense found with id {expense_id}."}

    current = dict(row)

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

    conn.execute(
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
    row = conn.execute(
        "SELECT * FROM expenses WHERE id = ?", (expense_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return {"error": f"No expense found with id {expense_id}."}

    expense = dict(row)
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
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
    rows = conn.execute(query, params).fetchall()
    conn.close()

    expenses = [dict(r) for r in rows]
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
    rows = conn.execute(
        """SELECT category,
                  ROUND(SUM(amount), 2) AS total,
                  COUNT(*)              AS count
           FROM expenses
           WHERE date BETWEEN ? AND ?
           GROUP BY category
           ORDER BY total DESC""",
        (start, end),
    ).fetchall()
    conn.close()

    breakdown = [dict(r) for r in rows]
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
    existing = conn.execute(
        """SELECT id FROM recurring_expenses
           WHERE category = ? AND description = ?""",
        (resolved, description.strip()),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE recurring_expenses
               SET amount = ?, currency = ?, day_of_month = ?, active = 1
               WHERE id = ?""",
            (amount, currency, day_of_month, existing["id"]),
        )
        action = "Updated"
        record_id = existing["id"]
    else:
        cursor = conn.execute(
            """INSERT INTO recurring_expenses
               (category, amount, currency, description, day_of_month, active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (resolved, amount, currency, description.strip(), day_of_month),
        )
        action = "Created"
        record_id = cursor.lastrowid

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
    rows = conn.execute(
        """SELECT * FROM recurring_expenses WHERE active = 1
           ORDER BY category, description""",
    ).fetchall()
    conn.close()

    templates = [dict(r) for r in rows]
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
    templates = conn.execute(
        "SELECT * FROM recurring_expenses WHERE active = 1"
    ).fetchall()

    added = []
    skipped = []

    for t in templates:
        # Clamp day to last day of month (handles Feb, 30-day months)
        try:
            expense_date = date(year, month, t["day_of_month"]).isoformat()
        except ValueError:
            # day_of_month > days in month — use last day
            last_day = (date(year, month + 1, 1) - timedelta(days=1)).day if month < 12 else 31
            expense_date = date(year, month, last_day).isoformat()

        # Idempotency check
        exists = conn.execute(
            """SELECT id FROM expenses
               WHERE category = ? AND description = ? AND date = ?""",
            (t["category"], t["description"], expense_date),
        ).fetchone()

        if exists:
            skipped.append(t["description"])
            continue

        conn.execute(
            """INSERT INTO expenses (amount, currency, category, description, date)
               VALUES (?, ?, ?, ?, ?)""",
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
    """Export expenses to an Excel (.xlsx) file for a given date range.

    The file has 5 columns: Day, Date, Category, Item, Cost in euros.
    It is saved to the exports/ directory and can be opened directly
    in Excel or imported into Google Sheets.

    Args:
        start_date: Start of range in YYYY-MM-DD format (inclusive).
            Defaults to the first day of the current month.
        end_date: End of range in YYYY-MM-DD format (inclusive).
            Defaults to today.

    Returns:
        A dict with the file path, row count, and instructions to find the file.
    """
    today = date.today()
    if start_date is None:
        start_date = today.replace(day=1).isoformat()
    if end_date is None:
        end_date = today.isoformat()

    conn = _get_connection()
    rows = conn.execute(
        """SELECT date, amount, currency, category, description
           FROM expenses
           WHERE date BETWEEN ? AND ?
           ORDER BY date ASC, id ASC""",
        (start_date, end_date),
    ).fetchall()
    conn.close()

    if not rows:
        return {
            "message": f"No expenses found between {start_date} and {end_date}.",
            "file_path": None,
            "row_count": 0,
        }

    os.makedirs(EXPORTS_DIR, exist_ok=True)
    filename = f"expenses_{start_date}_to_{end_date}.xlsx"
    file_path = os.path.join(EXPORTS_DIR, filename)
    abs_path = os.path.abspath(file_path)

    # Build workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Expenses"

    # Header style
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2E75B6")
    header_alignment = Alignment(horizontal="center")

    headers = ["Day", "Date", "Category", "Item", "Cost in euros"]
    col_widths = [12, 14, 20, 35, 16]

    for col_idx, (header, width) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        ws.column_dimensions[cell.column_letter].width = width

    # Data rows
    for row in rows:
        expense_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
        day_name = expense_date.strftime("%A")          # e.g. Monday
        date_fmt = expense_date.strftime("%d %b %Y")   # e.g. 01 Nov 2026
        cost = row["amount"]

        ws.append([day_name, date_fmt, row["category"], row["description"], cost])

    # Format cost column as euros
    for row_idx in range(2, ws.max_row + 1):
        ws.cell(row=row_idx, column=5).number_format = '€#,##0.00'

    # Freeze header row
    ws.freeze_panes = "A2"

    wb.save(file_path)

    return {
        "file_path": abs_path,
        "row_count": len(rows),
        "period": f"{start_date} to {end_date}",
        "message": (
            f"Your Excel file is ready with {len(rows)} expense(s) "
            f"for {start_date} to {end_date}.\n\n"
            f"📂 To find it on your Mac:\n"
            f"1. Open Finder\n"
            f"2. Press Cmd + Shift + G\n"
            f"3. Paste this path: {abs_path}\n\n"
            f"You can then open it in Excel or upload it to Google Sheets."
        ),
    }
