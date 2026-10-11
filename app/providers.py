"""What each review site can actually provide, stated before any reviews exist.

Yelp's public API returns at most three review excerpts per business and its caching
guidance limits stored content to 24 hours. See docs/architecture.md (Research boundaries).
"""

CAPABILITIES = {
    "yelp": {
        "provider": "yelp",
        "reviews": "sample",
        "max_reviews_per_fetch": 3,
        "full_history": False,
        "deletion_detection": False,
        "reply_posting": False,
        "content_max_age_hours": 24,
    },
}
