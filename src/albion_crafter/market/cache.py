from __future__ import annotations

from collections.abc import Sequence

from albion_crafter.database.database import MarketPriceRepository

from .aodp import (
    AODPClient,
    BatchFetchResult,
    BatchProgressCallback,
    CancellationCheck,
)


class CachedMarketService:
    def __init__(self, client: AODPClient, repository: MarketPriceRepository) -> None:
        self.client = client
        self.repository = repository

    def refresh(
        self,
        item_ids: Sequence[str],
        *,
        cities: Sequence[str],
        qualities: Sequence[int] = (1,),
        is_cancelled: CancellationCheck | None = None,
        on_progress: BatchProgressCallback | None = None,
    ) -> BatchFetchResult:
        """Refresh sequentially, committing each successful batch immediately."""
        return self.client.fetch_prices_batched(
            item_ids,
            cities=cities,
            qualities=qualities,
            is_cancelled=is_cancelled,
            on_batch_success=self.repository.upsert_many,
            on_progress=on_progress,
        )
