from __future__ import annotations

from collections.abc import Sequence

from albion_crafter.database.database import MarketPriceRepository

from .aodp import (
    AODPClient,
    BatchFetchResult,
    BatchProgressCallback,
    BatchSuccessCallback,
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
        on_batch_success: BatchSuccessCallback | None = None,
    ) -> BatchFetchResult:
        """Refresh sequentially, committing each successful batch immediately."""

        def persist(records) -> None:
            self.repository.upsert_many(records)
            if on_batch_success is not None:
                on_batch_success(records)

        return self.client.fetch_prices_batched(
            item_ids,
            cities=cities,
            qualities=qualities,
            is_cancelled=is_cancelled,
            on_batch_success=persist,
            on_progress=on_progress,
        )
