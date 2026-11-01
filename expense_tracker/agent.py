"""Expense Tracker agent — root LlmAgent definition."""

from google.adk.agents import LlmAgent

from . import prompt
from .tools import (
    add_expense,
    apply_recurring_expenses,
    delete_expense,
    edit_expense,
    export_expenses,
    get_monthly_summary,
    get_summary,
    list_expenses,
    list_recurring_expenses,
    set_recurring_expense,
)

MODEL = "gemini-3.5-flash-lite"

expense_tracker_agent = LlmAgent(
    name="expense_tracker",
    model=MODEL,
    description=(
        "A friendly conversational agent that helps you track daily expenses, "
        "review spending by category, manage recurring bills, and export monthly reports."
    ),
    instruction=prompt.EXPENSE_TRACKER_PROMPT,
    tools=[
        add_expense,
        edit_expense,
        delete_expense,
        list_expenses,
        get_summary,
        get_monthly_summary,
        set_recurring_expense,
        list_recurring_expenses,
        apply_recurring_expenses,
        export_expenses,
    ],
)

root_agent = expense_tracker_agent
