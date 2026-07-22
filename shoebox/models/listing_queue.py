from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ListingQueueRow(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # --- IDs / keys from your query ---
    card_id: str | None = None
    checklist_id: str | None = None
    subset_id: str | None = None
    parallel_id: str | None = None

    # --- checklist fields ---
    set_year: str | None = None
    set_name: str = ""
    subset_name: str = ""
    subset_type: str | None = None
    derived_card_number: str | None = None
    card_number: str = ""
    player: str | None = None
    team: str | None = None
    note: str | None = None
    note_check: str | None = None

    # --- parallel fields ---
    parallel_variety: str | None = ""  # optional in UI, but keep as string for dropdown
    print_run: int | None = None
    parallel_note: str | None = Field(default=None, description="p.other_note AS parallel_note")

    # --- queue / listing inputs ---
    quantity: int = 1
    price: float = 0.0
    image_front: str = ""
    image_back: str = ""

    @property
    def price_scrape_query(self) -> str:
        query_str = ""
        query_str += f"{self.set_name}"
        if self.subset_type == "Insert":
            query_str += f" {self.subset_name}"
        if self.parallel_variety:
            query_str += f" {self.parallel_variety}"
        query_str += f" {self.player}"
        return query_str
