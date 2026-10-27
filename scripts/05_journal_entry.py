"""Add storms, model forecasts and observed totals to the snow journal, interactively.

Run as many times as you like: new entries are added, existing ones (same storm / same model+init) are updated,
and the previous CSV is kept as *.csv.bak. Close the journal CSVs in Excel first.

    python scripts/05_journal_entry.py
In a notebook cell:
    from cottonwoods.journal_entry import run_interactive
    run_interactive()
"""
# All the logic is in src/cottonwoods/journal_entry.py. This file just starts the menu so you can run it
# from a terminal.
from cottonwoods.journal_entry import run_interactive

run_interactive()
