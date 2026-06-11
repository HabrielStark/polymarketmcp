"""Shared fixtures for the Hermes-PM test suite."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from hermes_pm.audit.store import AuditStore
from hermes_pm.config import RiskPolicy, load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.data.cache import OrderBookCache
from hermes_pm.events import EventBus
from hermes_pm.execution.paper_engine import PaperEngine
from hermes_pm.models import BookLevel, OrderBookSnapshot, Signal, SourceType
from hermes_pm.persistence.db import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        data_dir=str(tmp_path), db_filename="test.sqlite3",
        ws_reconnect_stale_ms=60_000, reconcile_interval_ms=60_000,
    )


@pytest.fixture
def policy():
    return RiskPolicy()


@pytest.fixture
def audit(db):
    return AuditStore(db)


@pytest.fixture
def paper_engine(db, audit, policy):
    return PaperEngine(db, OrderBookCache(), EventBus(), audit, policy)


@pytest.fixture
def book_factory():
    def make(token_id="tok", bid=0.49, ask=0.51, size=500.0, levels=4):
        bids = [BookLevel(price=round(bid - i * 0.01, 2), size=size) for i in range(levels)
                if bid - i * 0.01 > 0]
        asks = [BookLevel(price=round(ask + i * 0.01, 2), size=size) for i in range(levels)
                if ask + i * 0.01 < 1]
        return OrderBookSnapshot(token_id=token_id, bids=bids, asks=asks, last_trade=(bid + ask) / 2)
    return make


@pytest.fixture
def primary_evidence():
    def make(market_id="m"):
        return [Signal(market_id=market_id, source_type=SourceType.PRIMARY, source_ref="off://1",
                       text_summary="official source", trust_score=0.9)]
    return make


@pytest_asyncio.fixture
async def daemon(settings):
    d = TradingDaemon(settings)
    await d.start()
    await asyncio.sleep(0.25)
    try:
        yield d
    finally:
        await d.stop()


@pytest_asyncio.fixture
async def populated(daemon):
    """A daemon with one campaign that has discovered markets and one paper trade."""
    from hermes_pm.cli import _scripted_campaign

    # daemon already started; _scripted_campaign also starts (idempotent) and trades.
    cid = await _scripted_campaign(daemon)
    return daemon, cid
