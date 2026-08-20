from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from time import monotonic

from albion_crafter.core.models import Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.database.database import MarketPriceRepository

from .aodp import (
    AODPClient,
    BatchFetchResult,
    BatchProgress,
    CancellationCheck,
    Clock,
)
from .cache import CachedMarketService
from .models import MarketPrice, MarketSide, Region

ClientFactory = Callable[[Region], AODPClient]
CacheServiceFactory = Callable[[AODPClient, MarketPriceRepository], CachedMarketService]
RecipeRefreshProgressCallback = Callable[["RecipePriceRefreshProgress"], None]
_NetworkKey = tuple[Region, str, str, int]


class RecipePriceRole(StrEnum):
    MATERIAL = "material"
    OUTPUT = "output"


class RecipePriceAvailabilityStatus(StrEnum):
    UPDATED = "updated"
    RETAINED = "retained"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class RecipePriceRefreshRequest:
    """The exact current-price evidence needed by one calculator recipe.

    Material inputs are always Normal quality in the calculator. ``output_quality``
    applies only to the crafted output. AODP returns both order-book sides for a
    network key, so the side fields describe which persisted side the caller needs;
    they do not widen the HTTP request.
    """

    recipe: Recipe
    region: Region
    material_city: str
    sell_city: str
    output_quality: int
    material_side: MarketSide = MarketSide.SELL_ORDER
    output_side: MarketSide = MarketSide.SELL_ORDER

    def __post_init__(self) -> None:
        if not isinstance(self.recipe, Recipe):
            raise ValueError("recipe price refresh requires a Recipe")
        if not isinstance(self.region, Region):
            raise ValueError("recipe price refresh region must be a Region")
        for name in ("material_city", "sell_city"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if (
            isinstance(self.output_quality, bool)
            or not isinstance(self.output_quality, int)
            or not 1 <= self.output_quality <= 5
        ):
            raise ValueError("output_quality must be an integer between 1 and 5")
        for name in ("material_side", "output_side"):
            if not isinstance(getattr(self, name), MarketSide):
                raise ValueError(f"{name} must be a MarketSide")


@dataclass(frozen=True, slots=True)
class RecipePriceRequirement:
    role: RecipePriceRole
    region: Region
    item_id: str
    city: str
    quality: int
    side: MarketSide

    def __post_init__(self) -> None:
        if not isinstance(self.role, RecipePriceRole):
            raise ValueError("recipe price role must be a RecipePriceRole")
        if not isinstance(self.region, Region):
            raise ValueError("recipe price requirement region must be a Region")
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValueError("recipe price requirement item_id is required")
        if not isinstance(self.city, str) or not self.city.strip():
            raise ValueError("recipe price requirement city is required")
        if (
            isinstance(self.quality, bool)
            or not isinstance(self.quality, int)
            or not 1 <= self.quality <= 5
        ):
            raise ValueError("recipe price requirement quality must be between 1 and 5")
        if not isinstance(self.side, MarketSide):
            raise ValueError("recipe price requirement side must be a MarketSide")

    @property
    def network_key(self) -> _NetworkKey:
        return (self.region, self.item_id, self.city, self.quality)


@dataclass(frozen=True, slots=True)
class RecipePriceFetchGroup:
    region: Region
    city: str
    quality: int
    item_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.region, Region):
            raise ValueError("recipe price fetch group region must be a Region")
        if not isinstance(self.city, str) or not self.city.strip():
            raise ValueError("recipe price fetch group city is required")
        if (
            isinstance(self.quality, bool)
            or not isinstance(self.quality, int)
            or not 1 <= self.quality <= 5
        ):
            raise ValueError("recipe price fetch group quality must be between 1 and 5")
        if not self.item_ids or any(
            not isinstance(item_id, str) or not item_id.strip() for item_id in self.item_ids
        ):
            raise ValueError("recipe price fetch group needs non-empty item IDs")
        folded = [item_id.casefold() for item_id in self.item_ids]
        if len(folded) != len(set(folded)):
            raise ValueError("recipe price fetch group item IDs must be unique")

    @property
    def network_keys(self) -> tuple[_NetworkKey, ...]:
        return tuple((self.region, item_id, self.city, self.quality) for item_id in self.item_ids)


@dataclass(frozen=True, slots=True)
class RecipePriceRefreshPlan:
    request: RecipePriceRefreshRequest
    requirements: tuple[RecipePriceRequirement, ...]
    groups: tuple[RecipePriceFetchGroup, ...]
    batches_planned: int
    max_url_bytes: int

    def __post_init__(self) -> None:
        if not self.requirements or not self.groups:
            raise ValueError("recipe price refresh plan cannot be empty")
        if (
            isinstance(self.batches_planned, bool)
            or not isinstance(self.batches_planned, int)
            or self.batches_planned < 1
        ):
            raise ValueError("recipe price refresh batches_planned must be positive")
        if (
            isinstance(self.max_url_bytes, bool)
            or not isinstance(self.max_url_bytes, int)
            or self.max_url_bytes < 1
        ):
            raise ValueError("recipe price refresh max_url_bytes must be positive")
        expected = {_folded_network_key(value.network_key) for value in self.requirements}
        actual = [_folded_network_key(key) for group in self.groups for key in group.network_keys]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError(
                "recipe price fetch groups must cover each required network key exactly once"
            )

    @property
    def network_keys(self) -> tuple[_NetworkKey, ...]:
        return tuple(key for group in self.groups for key in group.network_keys)


@dataclass(frozen=True, slots=True)
class RecipePriceRefreshBatchOutcome:
    group_number: int
    group: RecipePriceFetchGroup
    result: BatchFetchResult


@dataclass(frozen=True, slots=True)
class RecipePriceRefreshFailure:
    group_number: int
    batch_number: int
    region: Region
    city: str
    quality: int
    item_ids: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class RecipePriceRefreshRecordFailure:
    group_number: int
    batch_number: int
    row_number: int
    region: Region
    city: str
    quality: int
    message: str


@dataclass(frozen=True, slots=True)
class RecipePriceAvailability:
    requirement: RecipePriceRequirement
    status: RecipePriceAvailabilityStatus
    price: int | None
    observed_at: datetime | None
    fetched_at: datetime | None
    provenance: Provenance

    @property
    def is_available(self) -> bool:
        return self.status is not RecipePriceAvailabilityStatus.MISSING


@dataclass(frozen=True, slots=True)
class RecipePriceRefreshProgress:
    groups_planned: int
    groups_completed: int
    batches_planned: int
    batches_completed: int
    batches_succeeded: int
    batches_failed: int
    request_attempts: int
    retry_count: int
    records_loaded: int
    record_failures: int
    current_group: RecipePriceFetchGroup
    current_batch_item_ids: tuple[str, ...]
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class RecipePriceRefreshResult:
    plan: RecipePriceRefreshPlan
    outcomes: tuple[RecipePriceRefreshBatchOutcome, ...]
    availability: tuple[RecipePriceAvailability, ...]
    groups_completed: int
    batches_completed: int
    batches_succeeded: int
    batches_failed: int
    request_attempts: int
    retry_count: int
    records_loaded: int
    record_failures: int
    elapsed_seconds: float
    cancelled: bool = False

    @property
    def requirements_requested(self) -> int:
        return len(self.plan.requirements)

    @property
    def network_keys_requested(self) -> int:
        return len(self.plan.network_keys)

    @property
    def groups_planned(self) -> int:
        return len(self.plan.groups)

    @property
    def batches_planned(self) -> int:
        return self.plan.batches_planned

    @property
    def max_url_bytes(self) -> int:
        return self.plan.max_url_bytes

    @property
    def failures(self) -> tuple[RecipePriceRefreshFailure, ...]:
        return tuple(
            RecipePriceRefreshFailure(
                outcome.group_number,
                failure.batch_number,
                outcome.group.region,
                outcome.group.city,
                outcome.group.quality,
                failure.item_ids,
                failure.message,
            )
            for outcome in self.outcomes
            for failure in outcome.result.failures
        )

    @property
    def record_failure_details(self) -> tuple[RecipePriceRefreshRecordFailure, ...]:
        return tuple(
            RecipePriceRefreshRecordFailure(
                outcome.group_number,
                failure.batch_number,
                failure.row_number,
                outcome.group.region,
                outcome.group.city,
                outcome.group.quality,
                failure.message,
            )
            for outcome in self.outcomes
            for failure in outcome.result.record_failures
        )

    @property
    def selected_sides_available(self) -> int:
        return sum(value.is_available for value in self.availability)

    @property
    def missing_requirements(self) -> tuple[RecipePriceRequirement, ...]:
        return tuple(value.requirement for value in self.availability if not value.is_available)

    @property
    def selected_sides_missing(self) -> int:
        return len(self.missing_requirements)

    @property
    def has_errors(self) -> bool:
        return bool(self.batches_failed or self.record_failures)

    @property
    def is_complete(self) -> bool:
        return not self.cancelled and not self.has_errors and not self.missing_requirements

    @property
    def is_partial(self) -> bool:
        return self.batches_succeeded > 0 and not self.is_complete


class RecipePriceRefreshService:
    """Refresh only the current-price keys needed by one calculator recipe."""

    def __init__(
        self,
        repository: MarketPriceRepository,
        *,
        client_factory: ClientFactory = AODPClient,
        cache_service_factory: CacheServiceFactory = CachedMarketService,
        clock: Clock = monotonic,
    ) -> None:
        self.repository = repository
        self.client_factory = client_factory
        self.cache_service_factory = cache_service_factory
        self._clock = clock

    def plan(self, request: RecipePriceRefreshRequest) -> RecipePriceRefreshPlan:
        client = self._client(request.region)
        return self._plan_with_client(request, client)

    def refresh(
        self,
        request: RecipePriceRefreshRequest,
        *,
        is_cancelled: CancellationCheck | None = None,
        on_progress: RecipeRefreshProgressCallback | None = None,
    ) -> RecipePriceRefreshResult:
        client = self._client(request.region)
        plan = self._plan_with_client(request, client)
        cache_service = self.cache_service_factory(client, self.repository)
        before = self._load_rows(plan.network_keys)
        started_at = self._clock()
        outcomes: list[RecipePriceRefreshBatchOutcome] = []
        groups_completed = 0
        batches_completed = 0
        batches_succeeded = 0
        batches_failed = 0
        request_attempts = 0
        retry_count = 0
        records_loaded = 0
        record_failures = 0
        cancelled = False

        for group_number, group in enumerate(plan.groups, start=1):
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                break
            request_plan = client.plan_price_requests(
                group.item_ids,
                cities=(group.city,),
                qualities=(group.quality,),
            )
            local_successes = 0
            local_failures = 0
            local_attempts = 0
            local_retries = 0
            local_records = 0
            local_record_failures = 0

            def report(
                batch: BatchProgress,
                group: RecipePriceFetchGroup = group,
                group_batch_count: int = request_plan.batch_count,
                groups_completed_before: int = groups_completed,
                batches_completed_before: int = batches_completed,
                batches_succeeded_before: int = batches_succeeded,
                batches_failed_before: int = batches_failed,
                request_attempts_before: int = request_attempts,
                retry_count_before: int = retry_count,
                records_loaded_before: int = records_loaded,
                record_failures_before: int = record_failures,
            ) -> None:
                nonlocal local_successes
                nonlocal local_failures
                nonlocal local_attempts
                nonlocal local_retries
                nonlocal local_records
                nonlocal local_record_failures
                local_successes += int(batch.successful)
                local_failures += int(not batch.successful)
                local_attempts += batch.request_attempts
                local_retries += batch.retry_count
                local_records += batch.records_returned
                local_record_failures += len(batch.record_failures)
                if on_progress is not None:
                    on_progress(
                        RecipePriceRefreshProgress(
                            groups_planned=len(plan.groups),
                            groups_completed=groups_completed_before
                            + int(batch.completed_batches == group_batch_count),
                            batches_planned=plan.batches_planned,
                            batches_completed=batches_completed_before + batch.completed_batches,
                            batches_succeeded=batches_succeeded_before + local_successes,
                            batches_failed=batches_failed_before + local_failures,
                            request_attempts=request_attempts_before + local_attempts,
                            retry_count=retry_count_before + local_retries,
                            records_loaded=records_loaded_before + local_records,
                            record_failures=record_failures_before + local_record_failures,
                            current_group=group,
                            current_batch_item_ids=batch.item_ids,
                        )
                    )

            result = cache_service.refresh(
                group.item_ids,
                cities=(group.city,),
                qualities=(group.quality,),
                is_cancelled=is_cancelled,
                on_progress=report,
            )
            outcomes.append(RecipePriceRefreshBatchOutcome(group_number, group, result))
            if not result.cancelled:
                groups_completed += 1
            batches_completed += result.completed_batches
            batches_succeeded += result.successful_batches
            batches_failed += result.failed_batches
            request_attempts += result.request_attempts
            retry_count += result.retry_count
            records_loaded += result.records_returned
            record_failures += len(result.record_failures)
            cancelled = result.cancelled
            if cancelled:
                if on_progress is not None:
                    on_progress(
                        RecipePriceRefreshProgress(
                            groups_planned=len(plan.groups),
                            groups_completed=groups_completed,
                            batches_planned=plan.batches_planned,
                            batches_completed=batches_completed,
                            batches_succeeded=batches_succeeded,
                            batches_failed=batches_failed,
                            request_attempts=request_attempts,
                            retry_count=retry_count,
                            records_loaded=records_loaded,
                            record_failures=record_failures,
                            current_group=group,
                            current_batch_item_ids=(),
                            cancelled=True,
                        )
                    )
                break

        after = self._load_rows(plan.network_keys)
        availability = tuple(
            _availability(
                requirement,
                before.get(requirement.network_key),
                after.get(requirement.network_key),
            )
            for requirement in plan.requirements
        )
        return RecipePriceRefreshResult(
            plan=plan,
            outcomes=tuple(outcomes),
            availability=availability,
            groups_completed=groups_completed,
            batches_completed=batches_completed,
            batches_succeeded=batches_succeeded,
            batches_failed=batches_failed,
            request_attempts=request_attempts,
            retry_count=retry_count,
            records_loaded=records_loaded,
            record_failures=record_failures,
            elapsed_seconds=max(self._clock() - started_at, 0.0),
            cancelled=cancelled,
        )

    def _client(self, region: Region) -> AODPClient:
        client = self.client_factory(region)
        if client.region is not region:
            raise ValueError("recipe price refresh client factory returned the wrong region")
        return client

    def _plan_with_client(
        self,
        request: RecipePriceRefreshRequest,
        client: AODPClient,
    ) -> RecipePriceRefreshPlan:
        requirements = _requirements(request)
        groups = _groups(requirements)
        batches_planned = 0
        max_url_bytes = 0
        for group in groups:
            request_plan = client.plan_price_requests(
                group.item_ids,
                cities=(group.city,),
                qualities=(group.quality,),
            )
            if request_plan.batch_count > client.max_batches:
                raise ValueError(
                    f"recipe price group needs {request_plan.batch_count} batches; configured "
                    f"maximum is {client.max_batches}"
                )
            batches_planned += request_plan.batch_count
            max_url_bytes = max(max_url_bytes, request_plan.max_url_bytes)
        return RecipePriceRefreshPlan(
            request,
            requirements,
            groups,
            batches_planned,
            max_url_bytes,
        )

    def _load_rows(self, keys: tuple[_NetworkKey, ...]) -> dict[_NetworkKey, MarketPrice]:
        rows: dict[_NetworkKey, MarketPrice] = {}
        for region, item_id, city, quality in keys:
            record = self.repository.get(item_id, city, quality, region)
            if record is not None:
                rows[(region, item_id, city, quality)] = record
        return rows


def _requirements(
    request: RecipePriceRefreshRequest,
) -> tuple[RecipePriceRequirement, ...]:
    values = [
        RecipePriceRequirement(
            RecipePriceRole.MATERIAL,
            request.region,
            material.item_id,
            request.material_city,
            1,
            request.material_side,
        )
        for material in request.recipe.materials
    ]
    values.append(
        RecipePriceRequirement(
            RecipePriceRole.OUTPUT,
            request.region,
            request.recipe.output.item_id,
            request.sell_city,
            request.output_quality,
            request.output_side,
        )
    )
    unique: dict[tuple[object, ...], RecipePriceRequirement] = {}
    for value in values:
        key = (
            value.role,
            value.region,
            value.item_id.casefold(),
            value.city.casefold(),
            value.quality,
            value.side,
        )
        unique.setdefault(key, value)
    return tuple(unique.values())


def _groups(
    requirements: tuple[RecipePriceRequirement, ...],
) -> tuple[RecipePriceFetchGroup, ...]:
    grouped: dict[tuple[Region, str, int], tuple[str, list[str]]] = {}
    for value in requirements:
        key = (value.region, value.city.casefold(), value.quality)
        city, item_ids = grouped.setdefault(key, (value.city, []))
        if value.item_id.casefold() not in {item_id.casefold() for item_id in item_ids}:
            item_ids.append(value.item_id)
        grouped[key] = (city, item_ids)
    return tuple(
        RecipePriceFetchGroup(region, city, quality, tuple(item_ids))
        for (region, _city_key, quality), (city, item_ids) in grouped.items()
    )


def _availability(
    requirement: RecipePriceRequirement,
    before: MarketPrice | None,
    after: MarketPrice | None,
) -> RecipePriceAvailability:
    before_value = _selected_side(before, requirement.side)
    after_value = _selected_side(after, requirement.side)
    price, observed_at = after_value
    if price is None:
        status = RecipePriceAvailabilityStatus.MISSING
    elif after_value != before_value:
        status = RecipePriceAvailabilityStatus.UPDATED
    else:
        status = RecipePriceAvailabilityStatus.RETAINED
    return RecipePriceAvailability(
        requirement=requirement,
        status=status,
        price=price,
        observed_at=observed_at,
        fetched_at=after.fetched_at if after is not None else None,
        provenance=after.provenance if after is not None else Provenance.UNKNOWN,
    )


def _selected_side(
    record: MarketPrice | None,
    side: MarketSide,
) -> tuple[int | None, datetime | None]:
    if record is None:
        return None, None
    price = record.price_for_side(side)
    if price is None or price <= 0:
        return None, None
    return price, record.timestamp_for_side(side)


def _folded_network_key(key: _NetworkKey) -> tuple[Region, str, str, int]:
    region, item_id, city, quality = key
    return region, item_id.casefold(), city.casefold(), quality
