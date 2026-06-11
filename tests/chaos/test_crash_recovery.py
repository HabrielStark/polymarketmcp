"""Real crash-recovery: a process killed mid-transaction must leave NO partial
money state behind (NFR-REL-001/002).

This is stronger than in-process rollback: a child process writes a committed
baseline, opens a ``db.transaction()``, writes a bogus cash value + a partial
ledger row, then ``os._exit()`` BEFORE the transaction commits (so the context
manager's commit never runs). The parent then reopens the same database file and
asserts SQLite discarded the uncommitted writes — the baseline is intact and the
ledger is still balanced.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hermes_pm.execution.ledger import CASH, Ledger
from hermes_pm.persistence.db import Database

CID = "crash-cid"

# Runs in a CHILD process. Commits a baseline, then dies mid-transaction.
_CHILD = r"""
import os, sys
from hermes_pm.persistence.db import Database
from hermes_pm.execution.ledger import Ledger, Posting, CASH

path = sys.argv[1]
cid = "crash-cid"
db = Database(path)

# 1) Durable, committed baseline: cash = 1000 + opening ledger (sums to zero).
db.kv_set(f"paper_cash:{cid}", 1000.0)
Ledger(db, cid).post([Posting(CASH, 1000.0, "open"), Posting("equity", -1000.0, "open")])

# 2) Open a transaction, write a BOGUS cash value + a partial (unbalanced) ledger
#    row, then kill the process before the transaction commits.
with db.transaction():
    db.kv_set(f"paper_cash:{cid}", 999999.0)
    db.append_ledger(cid, "txn-bogus", CASH, 999999.0, 0.0, "uncommitted partial")
    sys.stdout.write("WROTE_UNCOMMITTED\n")
    sys.stdout.flush()
    os._exit(7)  # immediate death — the CM's commit never runs
"""


def test_uncommitted_transaction_is_discarded_after_process_kill(tmp_path):
    db_path = str(Path(tmp_path) / "crash.sqlite3")

    proc = subprocess.run(  # noqa: S603 - fixed interpreter + literal script, not untrusted input
        [sys.executable, "-c", _CHILD, db_path],
        capture_output=True, text=True, timeout=60,
    )
    # The child died mid-transaction via os._exit(7), after writing the baseline.
    assert proc.returncode == 7, proc.stderr
    assert "WROTE_UNCOMMITTED" in proc.stdout

    # Reopen the same file: SQLite recovery must drop the uncommitted writes.
    db = Database(db_path)
    try:
        cash = db.kv_get(f"paper_cash:{CID}")
        balances = Ledger(db, CID).balances()
        assert cash == 1000.0, f"uncommitted cash leaked through crash: {cash}"
        assert balances.get(CASH) == 1000.0, f"partial ledger row survived: {balances}"
        assert Ledger(db, CID).is_balanced()  # the bogus one-sided row is gone
        # No bogus transaction id persisted.
        assert all(row["txn_id"] != "txn-bogus" for row in db.list_ledger(CID))
    finally:
        db.close()


def test_committed_baseline_survives_when_child_exits_after_commit(tmp_path):
    # Control: if the child commits and THEN exits, the data must persist — proving
    # the test above isolates *uncommitted* loss, not a broken DB path.
    db_path = str(Path(tmp_path) / "ok.sqlite3")
    child = (
        "import sys\n"
        "from hermes_pm.persistence.db import Database\n"
        "from hermes_pm.execution.ledger import Ledger, Posting, CASH\n"
        "db = Database(sys.argv[1]); cid='crash-cid'\n"
        "with db.transaction():\n"
        "    db.kv_set(f'paper_cash:{cid}', 4242.0)\n"
        "    Ledger(db, cid).post([Posting(CASH, 4242.0, 'x'), Posting('equity', -4242.0, 'x')])\n"
        "db.close()\n"
    )
    proc = subprocess.run([sys.executable, "-c", child, db_path],  # noqa: S603 - literal script
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    db = Database(db_path)
    try:
        assert db.kv_get(f"paper_cash:{CID}") == 4242.0  # committed -> durable
        assert Ledger(db, CID).is_balanced()
    finally:
        db.close()
