"""
Radar — market intelligence module.

Phase 1: Crypto only. NFT, Memecoin, Forex, Stocks adapters and Liquidation
WebSockets ship in later phases. The infrastructure (cache, rate_limiter,
fetcher, alerts, digest, schema) is general enough to accept those later
without migration.

What runs today:
  • CoinGecko adapter (top-100 leaderboard + arbitrary-id batches).
  • In-memory TTL cache keyed by (asset_kind, asset_identifier).
  • Token-bucket per-API rate limiter.
  • Fetcher loop (5 min): one batched call per asset_kind covering the union
    of every guild's watchlist; result lands in cache.
  • Alerts dispatcher (chained to each fetch tick): movement_up,
    movement_down, volume_spike with per-(guild, asset, direction) 1-hour
    cooldown via radar_alerts_log lookup.
  • Daily digest scheduler (1 min check): posts per-guild when local time
    matches the guild's daily_time + timezone_offset.

What's reserved for later phases (schema only, no code yet):
  • Reservoir (NFT), DEXScreener (meme), Frankfurter (forex), Alpha Vantage
    (stocks) adapters.
  • Binance + Bybit Futures liquidation WebSockets + cross-exchange
    aggregation + 5-min cooldown alerts (radar_liquidations_window already
    exists for these).
"""

PHASE = 1
