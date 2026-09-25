"""
Ticker-based category inference for Kalshi markets.

Maps ticker prefixes to categories. Used to enrich markets that have
category='unknown' (359K+ markets from the public backfill API).
"""
from typing import Optional, Tuple


# Prefix → category mapping, ordered longest-first so more specific
# prefixes match before general ones (e.g., KXNCAAMB before KXNCAA).
TICKER_CATEGORY_MAP: list[Tuple[str, str]] = [
    # Sports — extended/parlay variants (must come before shorter prefixes)
    ("KXMVESPORTSMULTIGAMEEXTENDED", "Sports"),  # 356K+ markets
    ("KXMVENBASINGLEGAME", "Sports"),
    ("KXMVENBAGAME", "Sports"),
    ("KXMVENBA", "Sports"),
    ("KXMVENHL", "Sports"),
    ("KXTABLETENNIS", "Sports"),
    ("KXAHLGAME", "Sports"),     # AHL hockey
    ("KXKHLGAME", "Sports"),     # KHL hockey
    ("KXLOLGAME", "Sports"),     # League of Legends esports
    ("KXDOTA2GAME", "Sports"),   # Dota 2 esports
    ("KXVALORANTMAP", "Sports"), # Valorant esports
    ("KXVALORANTGAME", "Sports"),
    ("KXCSGAME", "Sports"),      # CS2 esports

    # Sports — specific leagues
    ("KXNCAAMB", "Sports"),
    ("KXNCAA", "Sports"),
    ("KXNFL", "Sports"),
    ("KXNBA", "Sports"),
    ("KXMLB", "Sports"),
    ("KXNHL", "Sports"),
    ("KXSOCCER", "Sports"),
    ("KXTENNIS", "Sports"),
    ("KXMMA", "Sports"),
    ("KXGOLF", "Sports"),
    ("KXPGA", "Sports"),
    ("KXMASTERS", "Sports"),
    ("KXUSOPEN", "Sports"),
    ("KXOPEN", "Sports"),
    ("KXPGACHAMP", "Sports"),
    ("KXPLAYERS", "Sports"),
    ("KXSPORTS", "Sports"),
    ("KXMVESPORTS", "Sports"),
    ("KXMVES", "Sports"),  # Sports parlay prefix (288K markets)
    ("KXF1", "Sports"),
    ("KXNASCAR", "Sports"),
    ("KXBOXING", "Sports"),
    ("KXWRESTLING", "Sports"),
    ("KXCRICKET", "Sports"),
    ("KXRUGBY", "Sports"),
    ("KXOLYMPIC", "Sports"),
    ("KXCFB", "Sports"),  # College football

    # Crypto
    ("KXBTC", "Crypto"),
    ("KXETH", "Crypto"),
    ("KXSOL", "Crypto"),
    ("KXQUICKSETTLE", "Crypto"),
    ("KXQUICK", "Crypto"),
    ("KXCRYPTO", "Crypto"),

    # Weather / Climate
    ("KXHMONTHRANGE", "Climate and Weather"),
    ("KXHIGH", "Climate and Weather"),
    ("KXLOW", "Climate and Weather"),
    ("KXTEMP", "Climate and Weather"),
    ("KXLOWTNYC", "Climate and Weather"),
    ("KXLOWTMIA", "Climate and Weather"),
    ("KXLOWT", "Climate and Weather"),
    ("SNOWNY", "Climate and Weather"),
    ("SNOW", "Climate and Weather"),
    ("KXRAIN", "Climate and Weather"),
    ("KXWIND", "Climate and Weather"),
    ("KXHURRICANE", "Climate and Weather"),
    ("KXWILD", "Climate and Weather"),
    ("KXFIRE", "Climate and Weather"),
    ("KXAQI", "Climate and Weather"),

    # Economics
    ("KXGDP", "Economics"),
    ("KXCPI", "Economics"),
    ("KXFED", "Economics"),
    ("KXRATE", "Economics"),
    ("KXUNEMPLOY", "Economics"),
    ("KXJOBS", "Economics"),
    ("KXPCE", "Economics"),
    ("KXRECESSION", "Economics"),
    ("KXINFLATION", "Economics"),
    ("KXFOMC", "Economics"),
    ("KXSP500", "Economics"),
    ("KXNAS", "Economics"),
    ("KXDOW", "Economics"),
    ("KXSTOCK", "Economics"),
    ("KXINX", "Economics"),
    ("KXMARKET", "Economics"),

    # Politics / Government
    ("KXPRES", "Politics"),
    ("KXELECTION", "Politics"),
    ("KXSENATE", "Politics"),
    ("KXHOUSE", "Politics"),
    ("KXGOV", "Politics"),
    ("KXPOTUS", "Politics"),
    ("KXSCOTUS", "Politics"),
    ("KXCONGRESS", "Politics"),
    ("KXPOPE", "Politics"),
    ("KXNEWPOPE", "Politics"),
    ("KXTRUMP", "Politics"),
    ("KXBIDEN", "Politics"),

    # Science / Tech
    ("KXAI", "Science and Technology"),
    ("KXSPACE", "Science and Technology"),
    ("KXNASA", "Science and Technology"),
    ("KXEARTHQUAKE", "Science and Technology"),
    ("KXTSLA", "Science and Technology"),
    ("KXAPPL", "Science and Technology"),
    ("KXTECH", "Science and Technology"),

    # Entertainment / Media
    ("KXOSCARS", "Entertainment"),
    ("KXEMMY", "Entertainment"),
    ("KXGRAMMY", "Entertainment"),
    ("KXMOVIE", "Entertainment"),
    ("KXTV", "Entertainment"),
    ("KXNETFLIX", "Entertainment"),
    ("KXMEDIA", "Entertainment"),
    ("KXCELEBRITY", "Entertainment"),
    ("KXENTERTAINER", "Entertainment"),

    # Health
    ("KXCOVID", "Health"),
    ("KXFLU", "Health"),
    ("KXVIRUS", "Health"),
    ("KXPANDEMIC", "Health"),
    ("KXFDA", "Health"),
    ("KXBIRD", "Health"),
]


def infer_category(ticker: str) -> Optional[str]:
    """
    Infer market category from ticker prefix.

    Uses longest-prefix-first matching to handle overlapping prefixes
    (e.g., KXNCAAMB matches before KXNCAA).

    Args:
        ticker: Market ticker string (e.g., "KXNFL-SUPERBOWL-YES").

    Returns:
        Category string if a match is found, None otherwise.
    """
    ticker_upper = ticker.upper()
    for prefix, category in TICKER_CATEGORY_MAP:
        if ticker_upper.startswith(prefix):
            return category
    return None


def infer_category_from_title(title: str) -> Optional[str]:
    """
    Fallback: infer category from market title keywords.

    Used when ticker prefix doesn't match any known pattern.

    Args:
        title: Market title string.

    Returns:
        Category string if a keyword match is found, None otherwise.
    """
    title_lower = title.lower()

    title_keywords = {
        "Sports": [
            "nfl", "nba", "mlb", "nhl", "super bowl", "touchdown",
            "goals", "points", "rebounds", "assists", "strikeout",
            "home run", "quarterback", "pitcher", "three-pointer",
            "slam dunk", "penalty", "soccer", "football", "basketball",
            "baseball", "hockey", "tennis", "golf", "mma", "ufc",
            "boxing", "wrestling", "f1", "nascar", "pga",
        ],
        "Climate and Weather": [
            "temperature", "high temp", "low temp", "snowfall",
            "rainfall", "hurricane", "tornado", "wildfire", "drought",
            "heat wave", "cold snap", "wind speed", "air quality",
        ],
        "Economics": [
            "gdp", "cpi", "inflation", "interest rate", "federal reserve",
            "unemployment", "jobs report", "recession", "s&p 500",
            "nasdaq", "dow jones", "stock market",
        ],
        "Politics": [
            "president", "election", "senate", "congress", "governor",
            "supreme court", "white house", "democrat", "republican",
            "vote", "ballot", "primary",
        ],
        "Science and Technology": [
            "ai ", "artificial intelligence", "spacex", "nasa",
            "launch", "earthquake",
        ],
        "Entertainment": [
            "oscar", "emmy", "grammy", "box office", "netflix",
            "movie", "tv show", "streaming",
        ],
        "Health": [
            "covid", "flu", "virus", "pandemic", "fda",
            "bird flu", "vaccine",
        ],
    }

    for category, keywords in title_keywords.items():
        for keyword in keywords:
            if keyword in title_lower:
                return category

    return None


def get_category(ticker: str, title: str = "", existing_category: str = "") -> str:
    """
    Get the best category for a market.

    Priority:
    1. Existing non-unknown category from API
    2. Ticker prefix inference
    3. Title keyword inference
    4. 'unknown' fallback

    Args:
        ticker: Market ticker.
        title: Market title.
        existing_category: Current category from DB/API.

    Returns:
        Best available category string.
    """
    # If we already have a real category, keep it
    if existing_category and existing_category != "unknown":
        return existing_category

    # Try ticker prefix first (most reliable)
    category = infer_category(ticker)
    if category:
        return category

    # Try title keywords as fallback
    if title:
        category = infer_category_from_title(title)
        if category:
            return category

    return "unknown"
