from pydantic import Field

from .common import Model


class ChecklistRow(Model):
    # Natural key (whatever makes a row unique for you)
    set_name: str = Field(..., description="2025 Topps Chrome, 2020 Bowman")
    subset_name: str = Field(..., description="Base or Insert Name")
    subset_type: str = Field(..., description="Base or Insert")
    card_number: str = Field(..., description="")
    player: str = Field(..., description="")
    team: str | None = None
    note: str | None = None

    # ingested_at_utc: datetime = Field(default_factory=utc_now)


class ParallelRow(Model):
    # Depending on your normalization, you might key by set + subset
    set_name: str
    subset_name: str
    parallel_variety: str

    # Optional: print run / notes (helps pricing / rarity logic later)
    print_run: int | None = None
    other_note: str | None = None

    # ingested_at_utc: datetime = Field(default_factory=utc_now)
