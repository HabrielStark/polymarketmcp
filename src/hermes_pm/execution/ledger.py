"""Double-entry ledger (FR-PAPER-004).

Each fill produces a set of signed postings (positive = debit, negative =
credit) across CASH, POSITION:<token>, FEES, and REALIZED_PNL that **sum to
zero** — the double-entry invariant. Postings are persisted for audit, and the
invariant is asserted on every transaction so a simulation bug cannot silently
manufacture or destroy value."""

from __future__ import annotations

from dataclasses import dataclass

from hermes_pm.errors import StateError
from hermes_pm.persistence.db import Database
from hermes_pm.util.ids import new_id

CASH = "cash"
FEES = "fees"
REALIZED = "realized_pnl"


def position_account(token_id: str) -> str:
    return f"position:{token_id}"


@dataclass
class Posting:
    account: str
    amount: float  # signed: + debit, - credit
    memo: str = ""


class Ledger:
    def __init__(self, db: Database, campaign_id: str) -> None:
        self.db = db
        self.campaign_id = campaign_id

    def post(self, postings: list[Posting], tolerance: float = 1e-6) -> str:
        total = round(sum(p.amount for p in postings), 6)
        if abs(total) > tolerance:
            raise StateError(
                f"unbalanced ledger transaction (sum={total})", code="state_error",
                postings=[(p.account, p.amount) for p in postings],
            )
        txn_id = new_id("txn")
        for p in postings:
            if abs(p.amount) < tolerance:
                continue
            debit = p.amount if p.amount > 0 else 0.0
            credit = -p.amount if p.amount < 0 else 0.0
            self.db.append_ledger(
                self.campaign_id, txn_id, p.account, round(debit, 6), round(credit, 6), p.memo
            )
        return txn_id

    def balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in self.db.list_ledger(self.campaign_id):
            out[row["account"]] = round(
                out.get(row["account"], 0.0) + row["debit"] - row["credit"], 6
            )
        return out

    def is_balanced(self, tolerance: float = 1e-6) -> bool:
        return abs(round(sum(self.balances().values()), 6)) <= tolerance
