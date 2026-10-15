"""Normalization of raw provider reviews into ReviewBridge's own shape."""
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.providers import CAPABILITIES

YELP_EXCERPT_MAX_CHARS = CAPABILITIES["yelp"]["excerpt_max_chars"]
# Yelp's reference does not state the timezone of time_created. UTC is an assumption to
# verify if live data is ever evaluated (docs/architecture.md, Live Yelp decision).
YELP_TIME_ZONE = UTC


@dataclass(frozen=True)
class NormalizedReview:
    external_review_id: str
    rating: int
    text: str
    author_name: str
    review_url: str
    reviewed_at: datetime

    def content_hash(self) -> str:
        content = [self.rating, self.text, self.author_name, self.review_url,
                   self.reviewed_at.isoformat()]
        return hashlib.sha256(json.dumps(content).encode()).hexdigest()


def _text(value, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected non-empty text")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError("text too long")
    return value


def _yelp_review(raw: dict) -> NormalizedReview:
    rating = raw["rating"]
    if isinstance(rating, bool) or not isinstance(rating, int | float) or rating % 1:
        raise ValueError("rating must be a whole number")
    if not 1 <= rating <= 5:
        raise ValueError("rating out of range")
    reviewed_at = datetime.strptime(raw["time_created"], "%Y-%m-%d %H:%M:%S")
    url = _text(raw["url"], 2000)
    if not url.startswith("https://"):
        raise ValueError("review URL must be https")
    # Keep only the screen name; profile and photo URLs are personal data we do not need.
    return NormalizedReview(
        external_review_id=_text(raw["id"], 64),
        rating=int(rating),
        # Yelp already returns excerpts; cap defensively rather than reject.
        text=_text(raw["text"], 100_000)[:YELP_EXCERPT_MAX_CHARS],
        author_name=_text(raw["user"]["name"], 200),
        review_url=url,
        reviewed_at=reviewed_at.replace(tzinfo=YELP_TIME_ZONE),
    )


def normalize_yelp(raw_reviews: list) -> tuple[list[NormalizedReview], int]:
    """Returns valid reviews and the number rejected. Bad entries are rejected one at a time
    so a single malformed review never fails the batch; repeated IDs keep the first."""
    reviews, seen, rejected = [], set(), 0
    for raw in raw_reviews:
        try:
            review = _yelp_review(raw)
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue
        if review.external_review_id in seen:
            rejected += 1
            continue
        seen.add(review.external_review_id)
        reviews.append(review)
    return reviews, rejected
