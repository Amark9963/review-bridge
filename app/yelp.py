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

    def reviews(self, business_id: str) -> list[dict]:
        """Raw review objects exactly as Yelp's `reviews` response array shapes them."""
        ...


SYNTHETIC_REVIEWS = [
    (5, "Sam K.", "2026-03-02 14:05:00",
     "Friendly front desk and the cleaning was quick and thorough. Parking out back is "
     "easy. Would happily come back."),
    (4, "Priya N.", "2026-02-11 09:30:00",
     "Good care overall. I waited about twenty minutes past my appointment time, but the "
     "hygienist explained everything clearly."),
    (2, "Alex P.", "2026-01-20 17:45:00",
     "Billing was confusing and it took two calls to sort out a charge that should have "
     "been covered. The treatment itself was fine."),
]


class SyntheticYelp:
    """IDs starting with `rbtest` exist, `rbdown` simulates an outage, anything else is
    unknown. Shown details are invented and use the reserved `.invalid` domain."""

    def _check(self, business_id: str):
        if business_id.startswith("rbdown"):
            raise ProviderUnavailable()
        if not business_id.startswith("rbtest"):
            raise BusinessNotFound()

    def lookup(self, business_id: str) -> Business:
        self._check(business_id)
        suffix = business_id[-4:]
        return Business(id=business_id, name=f"Synthetic Business {suffix}",
                        address=f"{suffix} Example Street, Testville",
                        url=f"https://yelp.invalid/biz/{business_id}")

    def reviews(self, business_id: str) -> list[dict]:
        self._check(business_id)
        return [{
            "id": f"rv{business_id[6:]}{n}",
            "url": f"https://yelp.invalid/biz/{business_id}?hrid=rv{n}",
            "text": text,
            "rating": rating,
            "time_created": created,
            "user": {"id": f"user{n}", "profile_url": f"https://yelp.invalid/user/{n}",
                     "image_url": None, "name": name},
        } for n, (rating, name, created, text) in enumerate(SYNTHETIC_REVIEWS, start=1)]


def yelp_client() -> YelpClient:
    if settings().yelp_mode == "synthetic":
        return SyntheticYelp()
    raise NotImplementedError(settings().yelp_mode)
