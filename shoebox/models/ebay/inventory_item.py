from __future__ import annotations

import ast
from typing import Any

from pydantic import Field, field_validator

from ..common import Model

AspectValue = str | list[str]


class Dimensions(Model):
    height: float | None = None
    length: float | None = None
    width: float | None = None
    unit: str | None = None


class Weight(Model):
    unit: str | None = None
    value: float | None = None


class PackageWeightAndSize(Model):
    dimensions: Dimensions | None = None
    package_type: str | None = None
    shipping_irregular: bool | None = None
    weight: Weight | None = None


class ShipToLocationAvailability(Model):
    allocation_by_format: dict[str, int] | None = None
    availability_distributions: Any | None = None
    quantity: int | None = None


class Availability(Model):
    pickup_at_location_availability: Any | None = None
    ship_to_location_availability: ShipToLocationAvailability | None = None


class ConditionDescriptor(Model):
    additional_info: Any | None = None
    name: str | None = None
    values: list[str] = Field(default_factory=list)


class Product(Model):
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None

    brand: str | None = None
    mpn: str | None = None
    epid: str | None = None

    upc: str | None = None
    ean: str | None = None
    isbn: str | None = None

    image_urls: list[str] = Field(default_factory=list)
    video_ids: Any | None = None

    # Normalize here so everywhere else sees a dict[str, str|list[str]]
    aspects: dict[str, AspectValue] = Field(default_factory=dict)

    @field_validator("aspects", mode="before")
    @classmethod
    def normalize_aspects(cls, v: Any) -> dict[str, AspectValue]:
        """
        Normalize to dict[str, str | list[str]] with rules:
          - len(list)==1 -> scalar string
          - len(list)>1 -> list[str]
          - scalar -> string
          - empty/None -> "" (adjust to None if you prefer)
        Also parses stringified python dicts (single quotes) via ast.literal_eval.
        """
        d: dict[str, Any] = {}

        if v is None:
            d = {}
        elif isinstance(v, dict):
            d = v
        elif isinstance(v, str):
            try:
                parsed = ast.literal_eval(v)
                d = parsed if isinstance(parsed, dict) else {}
            except Exception:
                d = {}
        else:
            d = {}

        def to_str_list(raw_val: Any) -> list[str]:
            if raw_val is None:
                return []
            if isinstance(raw_val, (list, tuple, set)):
                out: list[str] = []
                for item in raw_val:
                    if item is None:
                        continue
                    s = str(item).strip()
                    if s:
                        out.append(s)
                return out
            s = str(raw_val).strip()
            return [s] if s else []

        out: dict[str, AspectValue] = {}
        for k, raw_val in d.items():
            key = str(k)
            vals = to_str_list(raw_val)

            if len(vals) == 0:
                out[key] = ""
            elif len(vals) == 1:
                out[key] = vals[0]
            else:
                out[key] = vals

        return out

    # Optional convenience helper
    def aspect_str(self, name: str) -> str | None:
        v = self.aspects.get(name)
        if v is None:
            return None
        if isinstance(v, list):
            return v[0] if v else None
        return v or None


class InventoryItem(Model):
    """
    Parse directly from the inner dict returned by getInventoryItem.
    """

    sku: str
    locale: str | None = None

    availability: Availability | None = None

    condition: str | None = None
    condition_description: Any | None = None
    condition_descriptors: list[ConditionDescriptor] = Field(default_factory=list)

    inventory_item_group_keys: Any | None = None
    package_weight_and_size: PackageWeightAndSize | None = None

    product: Product | None = None

    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> InventoryItem:
        # Preserve raw for debugging/auditing
        return cls.model_validate({**data, "raw": data})

    @property
    def aspects(self) -> dict[str, AspectValue]:
        """
        Convenience: access normalized aspects at the InventoryItem level.
        """
        return self.product.aspects if self.product else {}
