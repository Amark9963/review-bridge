"""Yelp adapter. Only the synthetic implementation exists until eligible live access is
verified (docs/architecture.md, next slice 5). Synthetic data is invented, never real."""
from dataclasses import dataclass
from typing import Protocol

from app.config import settings


@dataclass(frozen=True)
class Business:
    id: str
    name: str
    address: str
    url: str


class BusinessNotFound(Exception):
    pass


class ProviderUnavailable(Exception):
    pass


class YelpClient(Protocol):
    def lookup(self, business_id: str) -> Business: ...


class SyntheticYelp:
    """IDs starting with `rbtest` exist, `rbdown` simulates an outage, anything else is
    unknown. Shown details are invented and use the reserved `.invalid` domain."""

    def lookup(self, business_id: str) -> Business:
        if business_id.startswith("rbdown"):
            raise ProviderUnavailable()
        if not business_id.startswith("rbtest"):
            raise BusinessNotFound()
        suffix = business_id[-4:]
        return Business(id=business_id, name=f"Synthetic Business {suffix}",
                        address=f"{suffix} Example Street, Testville",
                        url=f"https://yelp.invalid/biz/{business_id}")


def yelp_client() -> YelpClient:
    if settings().yelp_mode == "synthetic":
        return SyntheticYelp()
    raise NotImplementedError(settings().yelp_mode)
