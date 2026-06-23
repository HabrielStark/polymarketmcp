"""Market discovery: normalize, filter, and decide tradability.

A market is tradable only if it is order-book enabled (FR-MD-002) AND has clear,
parseable resolution rules + source (FR-MD-003/004). Markets failing the latter
may still be tracked research-only. Filters implement FR-MD-005."""

from __future__ import annotations

from typing import Any

from hermes_pm.models import Market
from hermes_pm.util.timeutil import iso_to_ms


class DiscoveryEngine:
    @staticmethod
    def is_tradable(market: Market) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not market.enable_order_book:
            reasons.append("order_book_disabled")  # FR-MD-002
        if not market.has_clear_resolution:
            reasons.append("ambiguous_or_missing_resolution_rules")  # FR-MD-004
        return (not reasons, reasons)

    @staticmethod
    def passes_filters(market: Market, filters: dict[str, Any]) -> bool:
        cats = filters.get("categories")
        if cats and market.category.lower() not in {str(c).lower() for c in cats}:
            return False
        excluded = filters.get("exclude_categories") or []
        if market.category.lower() in {str(c).lower() for c in excluded}:
            return False
        tags_any = filters.get("tags_any")
        if tags_any and not ({str(t).lower() for t in tags_any} & {t.lower() for t in market.tags}):
            return False
        # FR-MD-005 microstructure filters. Liquidity/volume are treated strictly
        # (unknown == 0, so a thin/illiquid market is excluded). Spread is applied
        # only when a two-sided quote is known at discovery time; the risk engine
        # re-checks live spread against the policy gate at execution.
        min_liquidity = filters.get("min_liquidity")
        if min_liquidity is not None and (market.liquidity_usd or 0.0) < min_liquidity:
            return False
        min_volume = filters.get("min_volume")
        if min_volume is not None and (market.volume_usd or 0.0) < min_volume:
            return False
        max_spread = filters.get("max_spread")
        if max_spread is not None and market.spread is not None and market.spread > max_spread:
            return False
        if filters.get("require_order_book", True) and not market.enable_order_book:
            return False
        if filters.get("require_clear_resolution", True) and not market.has_clear_resolution:
            return False
        max_end = filters.get("max_end_time")
        if max_end and market.end_time:
            try:
                if iso_to_ms(market.end_time) > iso_to_ms(max_end):
                    return False
            except ValueError:
                return False
        return True

    @classmethod
    def build_watchlist(
        cls, markets: list[Market], filters: dict[str, Any] | None = None
    ) -> list[Market]:
        filters = filters or {}
        out = []
        for m in markets:
            if not cls.passes_filters(m, filters):
                continue
            tradable, _ = cls.is_tradable(m)
            if filters.get("require_tradable", True) and not tradable:
                continue
            out.append(m)
        return out
