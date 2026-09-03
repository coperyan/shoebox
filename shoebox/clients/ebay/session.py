"""Shared plumbing for the eBay REST sub-clients.

``build_session`` reads the app settings, constructs the ``ebay_rest`` SDK
object, and hands back the :class:`EbaySession` every sub-client shares. The
SDK object is wrapped in :class:`RestApi` so that every failure surfaces as
:class:`~.errors.EbayApiError` instead of ``ebay_rest.Error``.

The Trading API client is deliberately *not* part of the session: it needs a
separate Auth'n'Auth token and most pipelines never touch it, so
``EbayClient`` builds it lazily on first use.
"""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ebay_rest import API
from ebay_rest import Error as EbayRestError

from shoebox.settings import Settings, get_settings

from .errors import EbayApiError


def rest_config_file(settings: Settings | None = None) -> Path:
    """Path to ``ebay_rest.json``: ``EBAY_REST_CONFIG_PATH`` if set, else ``ebay.path``."""
    settings = settings or get_settings()
    env_override = os.getenv("EBAY_REST_CONFIG_PATH")
    if env_override:
        return Path(env_override)
    return Path(settings.ebay.path) / "ebay_rest.json"


class RestApi:
    """``ebay_rest.API`` with its errors translated to :class:`EbayApiError`.

    Attribute access is forwarded to the SDK object. Callables are wrapped so an
    ``ebay_rest.Error`` raised on the call, or while iterating one of the SDK's
    paginating generators, comes back as ``EbayApiError``. Non-callables pass
    straight through, so the private-attribute escape hatches used by
    ``StoresClient._invoke`` and ``BrowseClient.aspect_refinements`` keep
    working (their calls get translated too).
    """

    def __init__(self, api: Any):
        self._api = api

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._api, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                result = attr(*args, **kwargs)
            except EbayRestError as e:
                raise EbayApiError.from_rest_error(e) from e
            if inspect.isgenerator(result):
                return _translate_generator(result)
            return result

        return call


def _translate_generator(gen: Iterator[Any]) -> Iterator[Any]:
    try:
        yield from gen
    except EbayRestError as e:
        raise EbayApiError.from_rest_error(e) from e


def unwrap_records(response: Iterable[Any]) -> list[dict[str, Any]]:
    """Collect the ``record`` entries from an ``ebay_rest`` paginating response.

    The SDK yields data items as ``{"record": ...}`` interleaved with control
    entries (``{"total": n}``, ``{"warnings": ...}``); only the records are data.
    """
    return [x["record"] for x in response if isinstance(x, dict) and "record" in x]


@dataclass(frozen=True)
class EbaySession:
    api: RestApi
    settings: Settings


def build_rest_api(settings: Settings | None = None) -> RestApi:
    settings = settings or get_settings()
    config_file = rest_config_file(settings)
    if not config_file.exists():
        raise FileNotFoundError(
            f"Missing ebay_rest config file: {config_file}. "
            "Create one from configs/ebay_rest.example.json (or set EBAY_REST_CONFIG_PATH)."
        )
    api = API(
        application=settings.ebay.application,
        user=settings.ebay.user,
        header=settings.ebay.header,
        path=str(config_file.parent),
    )
    return RestApi(api)


def build_session(settings: Settings | None = None) -> EbaySession:
    settings = settings or get_settings()
    return EbaySession(api=build_rest_api(settings), settings=settings)
