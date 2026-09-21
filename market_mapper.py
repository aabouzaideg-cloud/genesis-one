"""
MarketMapper — two-level Polymarket → Capital.com epic resolver.

Level 1: Exact match by condition_id from market_map.json (confidence = 1.0)
Level 2: Weighted keyword scoring across all supported epics

A signal is only emitted if confidence >= MIN_CONFIDENCE.
Anything below threshold is dropped — no guessing.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("MarketMapper")

MAP_PATH     = os.path.join(os.path.dirname(__file__), "market_map.json")
MIN_CONFIDENCE = 0.35   # below this, the signal is dropped
RAW_SCORE_THRESHOLD = 0.7  # raw keyword weight sum needed to pass (not normalized)

# Patterns that indicate a short-term binary "Up or Down" market — excluded from keyword matching
# e.g. "Bitcoin Up or Down - May 5, 4:00AM-4:15AM ET"
import re as _re
_SHORT_TERM_BINARY = _re.compile(
    r"(up or down|up\/down).{0,10}(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}:\d{2})",
    _re.IGNORECASE,
)


@dataclass
class MappingResult:
    epic:       str
    confidence: float
    method:     str   # "curated" | "keyword" | "no_match"
    reason:     str


# ── Weighted keyword definitions per epic ─────────────────────────────────────
# Weight reflects how directly the keyword implies that epic is affected.
# 1.0 = definitive, 0.5 = strong signal, 0.3 = supporting context

EPIC_KEYWORDS: dict[str, dict[str, float]] = {
    "SILVER": {
        "silver":          1.0,
        "xag":             1.0,
        "silver price":    1.0,
        "precious metal":  0.6,
        "safe haven":      0.4,
        "gold silver":     0.7,
        "inflation":       0.4,
        "rate cut":        0.4,
        "fed pause":       0.4,
    },
    "NATGAS": {
        "natural gas":     1.0,
        "lng":             0.9,
        "liquified natural gas": 1.0,
        "gas supply":      0.8,
        "gas pipeline":    0.7,
        "henry hub":       1.0,
        "energy crisis":   0.5,
        "hormuz":          0.6,
        "strait of hormuz": 0.7,
        "iran gas":        0.8,
        "russia gas":      0.8,
        "gas price":       0.7,
    },
    "US30": {
        "dow jones":       1.0,
        "dow":             0.9,
        "djia":            1.0,
        "blue chip":       0.7,
        "industrial":      0.5,
        "federal reserve": 0.6,
        "fed rate":        0.7,
        "rate cut":        0.6,
        "recession":       0.5,
        "wall street":     0.5,
        "stock market":    0.5,
        "trump tariff":    0.6,
        "trade war":       0.6,
    },
    "DE30": {
        "dax":             1.0,
        "german":          0.8,
        "germany":         0.7,
        "deutsche":        0.7,
        "bundesbank":      0.8,
        "ecb":             0.7,
        "european":        0.5,
        "russia ukraine":  0.8,
        "ukraine ceasefire": 0.9,
        "russia war":      0.8,
        "nato":            0.7,
        "europe economy":  0.6,
        "eurozone":        0.7,
    },
    "EURUSD": {
        "eur/usd":       1.0,
        "eurusd":        1.0,
        "euro":          0.8,
        "ecb":           0.8,
        "european central bank": 0.8,
        "rate cut":      0.6,
        "rate hike":     0.6,
        "fed cuts":      0.5,
        "federal reserve": 0.5,
        "fomc":          0.5,
        "interest rate": 0.4,
        "dollar":        0.4,
        "usd":           0.3,
        "inflation":     0.2,
    },
    "BTCUSD": {
        "bitcoin":            1.0,
        "btc":                1.0,
        "bitcoin up or down": 1.0,
        "btc up or down":     1.0,
        "crypto":             0.7,
        "cryptocurrency":     0.7,
        "btc etf":            0.9,
        "bitcoin etf":        0.9,
        "coinbase":           0.5,
        "binance":            0.5,
        "satoshi":            0.6,
        "blockchain":         0.3,
        "halving":            0.8,
        "sec crypto":         0.6,
        "digital asset":      0.3,
    },
    "GOLD": {
        "gold":          1.0,
        "xau":           1.0,
        "cpi":           0.7,
        "inflation":     0.6,
        "consumer price": 0.6,
        "safe haven":    0.5,
        "recession":     0.4,
        "fed pause":     0.4,
        "rate pause":    0.4,
        "pce":           0.5,
        "core inflation": 0.6,
        "stagflation":   0.5,
    },
    "OIL_CRUDE": {
        "oil":              1.0,
        "crude":            1.0,
        "wti crude":        1.0,
        "wti crude oil":    1.0,
        "opec":             0.9,
        "brent":            0.9,
        "wti":              0.9,
        "energy":           0.5,
        "petroleum":        0.8,
        "gas price":        0.4,
        "oil supply":       0.8,
        "oil demand":       0.7,
        "energy crisis":    0.6,
        "saudi":            0.5,
        "hormuz":           0.9,
        "strait of hormuz": 1.0,
        "iran":             0.6,
        "lng":              0.7,
        "natural gas":      0.7,
    },
    "US500": {
        "s&p 500":           1.0,
        "s&p500":            1.0,
        "spx":               1.0,
        "sp500":             1.0,
        "spy":               0.9,
        "stock market":      0.6,
        "equities":          0.5,
        "bear market":       0.5,
        "bull market":       0.5,
        "recession":         0.7,
        "earnings":          0.4,
        "gdp":               0.3,
        "wall street":       0.4,
        "dow jones":         0.5,
        "federal reserve":   0.8,
        "fed rate":          0.9,
        "rate cut":          0.8,
        "rate hike":         0.8,
        "fomc":              0.9,
        "interest rate cut": 0.9,
        "interest rate hike":0.9,
        "fed pivot":         0.9,
        "powell":            0.7,
    },
    "US100": {
        "nasdaq":        1.0,
        "nasdaq 100":    1.0,
        "tech stocks":   0.8,
        "technology":    0.5,
        "qqq":           0.9,
        "nvidia":        0.6,
        "apple":         0.5,
        "microsoft":     0.5,
        "ai":            0.4,
        "semiconductor": 0.5,
        "big tech":      0.6,
        "mag 7":         0.6,
        "magnificent 7": 0.6,
    },
    "ETHUSD": {
        "ethereum":      1.0,
        "$eth":          1.0,
        "eth ":          0.9,
        "ether":         0.8,
        "eth etf":       0.9,
        "ethereum etf":  0.9,
        "vitalik":       0.7,
        "eth 2.0":       0.8,
        "eip":           0.6,
        "defi":          0.5,
    },
    "EURUSD": {
        "eur/usd":           1.0,
        "eurusd":            1.0,
        "euro":              0.8,
        "ecb":               0.8,
        "european central bank": 0.8,
        "rate cut":          0.4,
        "dollar index":      0.6,
        "dxy":               0.7,
        "russia ukraine":    0.7,
        "ukraine ceasefire": 0.8,
        "ukraine war end":   0.8,
        "russia sanctions":  0.6,
    },
    "USDJPY": {
        "usd/jpy":       1.0,
        "usdjpy":        1.0,
        "dollar yen":    1.0,
        "bank of japan": 0.9,
        "boj":           0.9,
        "yen":           0.8,
        "japanese yen":  0.9,
    },
    "GBPUSD": {
        "gbp/usd":       1.0,
        "gbpusd":        1.0,
        "pound dollar":  1.0,
        "sterling":      0.9,
        "bank of england": 0.9,
        "boe":           0.8,
        "british pound": 0.9,
    },
}

# Max possible score per epic (sum of all weights) — used for normalization
_MAX_SCORES = {
    epic: sum(weights.values())
    for epic, weights in EPIC_KEYWORDS.items()
}


class MarketMapper:
    def __init__(self):
        self._curated: dict[str, dict] = {}
        self._load_curated()

    def _load_curated(self):
        try:
            with open(MAP_PATH, "r") as f:
                data = json.load(f)
            raw = data.get("curated", {})
            # Skip separator entries (value is empty string)
            self._curated = {k: v for k, v in raw.items() if isinstance(v, dict)}
            log.info(f"Loaded {len(self._curated)} curated market mappings.")
        except Exception as exc:
            log.warning(f"Could not load market_map.json: {exc}")

    # ── Public Interface ──────────────────────────────────────────────────────

    def resolve(self, condition_id: str, question: str) -> MappingResult:
        """
        Resolve a Polymarket market to a Capital.com epic.
        Returns a MappingResult — caller should check confidence >= MIN_CONFIDENCE.
        """
        # Level 1: curated exact match
        result = self._level1_curated(condition_id)
        if result:
            return result

        # Level 2: keyword scoring — reject short-term "Up or Down" binary markets
        if _SHORT_TERM_BINARY.search(question):
            return MappingResult(
                epic="",
                confidence=0.0,
                method="no_match",
                reason=f"Short-term binary market excluded: '{question[:50]}'",
            )

        result = self._level2_keywords(question)
        if result:
            return result

        return MappingResult(
            epic="",
            confidence=0.0,
            method="no_match",
            reason=f"No mapping found for: '{question[:60]}' (id={condition_id[:12]}...)",
        )

    def is_tradeable(self, result: MappingResult) -> bool:
        return result.confidence >= MIN_CONFIDENCE and result.epic != ""

    # ── Level 1: Curated ─────────────────────────────────────────────────────

    def _level1_curated(self, condition_id: str) -> Optional[MappingResult]:
        entry = self._curated.get(condition_id)
        if not entry:
            return None
        return MappingResult(
            epic=entry["epic"],
            confidence=entry.get("confidence", 1.0),
            method="curated",
            reason=entry.get("description", condition_id),
        )

    # ── Level 2: Keyword Scoring ──────────────────────────────────────────────

    def _level2_keywords(self, question: str) -> Optional[MappingResult]:
        text = question.lower()
        raw_scores: dict[str, float] = {}

        for epic, keywords in EPIC_KEYWORDS.items():
            raw_score = sum(
                weight for phrase, weight in keywords.items()
                if phrase in text
            )
            raw_scores[epic] = raw_score

        best_epic  = max(raw_scores, key=lambda e: raw_scores[e])
        best_raw   = raw_scores[best_epic]

        if best_raw < RAW_SCORE_THRESHOLD:
            log.debug(f"Raw scores below threshold ({RAW_SCORE_THRESHOLD}): "
                      + ", ".join(f"{e}={s:.2f}" for e, s in raw_scores.items() if s > 0))
            return None

        # Normalise for the confidence field (capped at 1.0)
        best_score = min(best_raw / 2.0, 1.0)

        if best_score < MIN_CONFIDENCE:
            log.debug(
                f"Keyword scores below threshold ({MIN_CONFIDENCE}): "
                + ", ".join(f"{e}={s:.2f}" for e, s in scores.items() if s > 0)
            )
            return None

        matched_keywords = [kw for kw in EPIC_KEYWORDS[best_epic] if kw in text]
        return MappingResult(
            epic=best_epic,
            confidence=round(best_score, 3),
            method="keyword",
            reason=(
                f"Keyword match → {best_epic} (confidence={best_score:.0%}) "
                f"via: {', '.join(matched_keywords[:4])}"
            ),
        )

    def reload(self):
        """Hot-reload the curated map without restarting the bot."""
        self._load_curated()
