import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shoebox.clients.ebay.trading import TradingClient
from shoebox.settings import Settings, get_settings


class EbayClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class EbaySession:
    api: Any
    legacy_api: Any
    Error: Any
    settings: Settings

    def parse_error(self, e: Any) -> dict:
        try:
            d = json.loads(e.as_dict().get("detail"))
            return d.get("errors", [{}])[0]
        except Exception:
            return {"raw": str(e)}


def build_session(settings: Settings | None = None) -> EbaySession:
    settings = settings or get_settings()

    config_dir = Path(settings.ebay.path)
    config_file = config_dir / "ebay_rest.json"

    env_override = os.getenv("EBAY_REST_CONFIG_PATH")
    if env_override:
        config_file = Path(env_override)

    if not config_file.exists():
        raise FileNotFoundError(
            f"Missing ebay_rest config file: {config_file}. "
            "Create one from configs/ebay_rest.example.json (or set EBAY_REST_CONFIG_PATH)."
        )

    try:
        from ebay_rest import API, Error  # type: ignore
    except Exception as e:  # pragma: no cover
        raise EbayClientError(
            "The 'ebay_rest' package is not installed. "
            "Install it from https://github.com/matecsaj/ebay_rest (e.g. pip install -e .). "
            f"Original import error: {e}"
        ) from e

    api = API(
        application=settings.ebay.application,
        user=settings.ebay.user,
        header=settings.ebay.header,
        path=str(config_file.parent),
    )
    legacy_api = TradingClient()
    return EbaySession(api=api, legacy_api=legacy_api, Error=Error, settings=settings)
