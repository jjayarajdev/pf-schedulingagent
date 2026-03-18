"""Weather-aware scheduling for outdoor projects.

Ported from v1.2.9 weather_aware_scheduling.py.
Automatically checks weather conditions when scheduling outdoor projects
and enriches available dates with [GOOD], [WARN], [BAD] indicators.
"""

import logging
from datetime import datetime
from typing import Any

import httpx

from tools.weather import WEATHER_CODES, _geocode

logger = logging.getLogger(__name__)

# Category-specific weather thresholds for outdoor work.
# Keys are matched via substring (case-insensitive), so "Roofing" matches
# "Roofing Repair", "Metal Roofing", etc.
OUTDOOR_CATEGORIES: dict[str, dict[str, Any]] = {
    "Decking": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 40,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Roofing": {
        "bad_conditions": ["rain", "snow", "thunderstorm", "ice"],
        "rain_threshold": 30,
        "temp_min": 40,
        "temp_max": 90,
        "wind_max": 20,
    },
    "Siding": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 35,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Exterior Painting": {
        "bad_conditions": ["rain", "snow"],
        "rain_threshold": 20,
        "temp_min": 50,
        "temp_max": 90,
        "wind_max": 15,
    },
    "Fencing": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 32,
        "temp_max": 100,
        "wind_max": 30,
    },
    "Fence": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 32,
        "temp_max": 100,
        "wind_max": 30,
    },
    "Concrete": {
        "bad_conditions": ["rain", "snow", "thunderstorm", "ice"],
        "rain_threshold": 30,
        "temp_min": 40,
        "temp_max": 95,
        "wind_max": 20,
    },
    "Flooring": {
        "bad_conditions": ["rain", "snow"],
        "rain_threshold": 50,
        "temp_min": 40,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Windows": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 40,
        "temp_min": 35,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Doors": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 40,
        "temp_min": 35,
        "temp_max": 95,
        "wind_max": 25,
    },
    # Additional outdoor categories
    "Grill": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 35,
        "temp_max": 100,
        "wind_max": 25,
    },
    "Balcony": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 40,
        "temp_min": 35,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Patio": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 40,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Gutter": {
        "bad_conditions": ["rain", "snow", "thunderstorm", "ice"],
        "rain_threshold": 30,
        "temp_min": 35,
        "temp_max": 95,
        "wind_max": 20,
    },
    "Solar": {
        "bad_conditions": ["rain", "snow", "thunderstorm", "ice"],
        "rain_threshold": 30,
        "temp_min": 40,
        "temp_max": 95,
        "wind_max": 20,
    },
    "Awning": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 40,
        "temp_min": 35,
        "temp_max": 95,
        "wind_max": 20,
    },
    "Pergola": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 40,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Landscaping": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 32,
        "temp_max": 100,
        "wind_max": 30,
    },
    "Exterior": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 40,
        "temp_min": 35,
        "temp_max": 95,
        "wind_max": 25,
    },
    "Install": {
        "bad_conditions": ["rain", "snow", "thunderstorm"],
        "rain_threshold": 50,
        "temp_min": 40,
        "temp_max": 95,
        "wind_max": 25,
    },
}

# Default criteria for unrecognized outdoor categories
_DEFAULT_CRITERIA: dict[str, Any] = {
    "bad_conditions": ["rain", "snow", "thunderstorm"],
    "rain_threshold": 50,
    "temp_min": 40,
    "temp_max": 95,
    "wind_max": 25,
}

# Known indoor-only categories (never weather-dependent)
_INDOOR_CATEGORIES: set[str] = {
    "plumbing", "electrical", "hvac", "appliance", "carpet",
    "interior painting", "drywall", "cabinet", "countertop",
}


def is_outdoor_project(category: str, project_type: str = "") -> bool:
    """Check if a project category/type requires outdoor work.

    Checks both ``category`` and ``project_type`` against known outdoor
    categories.  If neither matches a known outdoor category, falls back
    to excluding known indoor-only categories — most field service work
    is outdoor by default.
    """
    if not category and not project_type:
        return False

    # Check indoor exclusions FIRST (takes priority)
    combined = f"{category} {project_type}".lower().strip()
    if any(indoor in combined for indoor in _INDOOR_CATEGORIES):
        return False

    # Check both fields against known outdoor categories
    for text in (category, project_type):
        if not text:
            continue
        text_lower = text.lower()
        if any(cat.lower() in text_lower for cat in OUTDOOR_CATEGORIES):
            return True

    # Default: treat as outdoor (most field service work is weather-dependent)
    return True


def _get_category_criteria(category: str) -> dict[str, Any]:
    """Get weather criteria for a category. Falls back to defaults."""
    if not category:
        return _DEFAULT_CRITERIA
    for cat, criteria in OUTDOOR_CATEGORIES.items():
        if cat.lower() in category.lower():
            return criteria
    return _DEFAULT_CRITERIA


def _analyze_suitability(
    forecast: dict, category: str,
) -> dict[str, Any]:
    """Analyze if weather is suitable for outdoor work on a given day.

    Returns ``{"suitable": bool, "warnings": [...], "severity": str, "recommendation": str}``.
    """
    if not forecast:
        return {"suitable": True, "warnings": [], "severity": "low", "recommendation": ""}

    criteria = _get_category_criteria(category)
    warnings: list[str] = []
    severity = "low"

    # --- condition ---
    condition = forecast.get("condition", "").lower()
    for bad in criteria["bad_conditions"]:
        if bad.lower() in condition:
            warnings.append(f"{forecast.get('condition', 'Unknown')} forecasted")
            severity = "high" if bad in ("thunderstorm", "ice", "snow") else "medium"

    # --- precipitation ---
    try:
        precip = float(forecast.get("precipitation", 0) or 0)
    except (ValueError, TypeError):
        precip = 0
    if precip >= criteria["rain_threshold"]:
        warnings.append(f"{int(precip)}% chance of precipitation")
        if precip >= 70:
            severity = "high"
        elif severity == "low":
            severity = "medium"

    # --- temperature ---
    try:
        max_temp = float(forecast.get("high_temp", 75) or 75)
        min_temp = float(forecast.get("low_temp", 60) or 60)
    except (ValueError, TypeError):
        max_temp, min_temp = 75, 60

    if max_temp < criteria["temp_min"]:
        warnings.append(f"Temperature too cold (high of {int(max_temp)}F)")
        if severity == "low":
            severity = "medium"

    if min_temp > criteria["temp_max"]:
        warnings.append(f"Temperature too hot (low of {int(min_temp)}F)")
        if severity == "low":
            severity = "medium"

    # --- wind ---
    try:
        wind = float(forecast.get("wind", 0) or 0)
    except (ValueError, TypeError):
        wind = 0
    if wind >= criteria["wind_max"]:
        warnings.append(f"High winds ({int(wind)} mph)")
        if wind >= criteria["wind_max"] + 10:
            severity = "high"

    suitable = len(warnings) == 0

    if not suitable:
        if severity == "high":
            recommendation = (
                f"We strongly recommend rescheduling. {category} work in these "
                "conditions could be unsafe or result in poor quality."
            )
        elif severity == "medium":
            recommendation = (
                f"Working conditions may not be ideal for {category}. "
                "Consider choosing a better day if possible."
            )
        else:
            recommendation = f"Minor weather concerns for {category} work. Proceed with caution."
    else:
        recommendation = "Weather conditions look good for outdoor work."

    return {
        "suitable": suitable,
        "warnings": warnings,
        "severity": severity,
        "recommendation": recommendation,
    }


async def _fetch_forecast(location: str, dates: list[str]) -> dict[str, dict] | None:
    """Fetch Open-Meteo forecast and return a date→forecast lookup.

    Returns ``{"2026-03-17": {"condition": "Clear sky", "high_temp": 72, ...}, ...}``
    or ``None`` on failure.
    """
    geo = await _geocode(location)
    if not geo:
        logger.warning("Weather enrichment: could not geocode '%s'", location)
        return None

    lat, lon = geo["latitude"], geo["longitude"]
    # Request enough days to cover all dates
    num_days = 16  # Open-Meteo max
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        f"precipitation_probability_max,wind_speed_10m_max"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
        f"&timezone=auto&forecast_days={num_days}"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url)
            res.raise_for_status()
            data = res.json()
    except Exception:
        logger.exception("Weather enrichment: forecast API failed")
        return None

    daily = data.get("daily", {})
    forecast_dates = daily.get("time", [])
    if not forecast_dates:
        return None

    lookup: dict[str, dict] = {}
    for i, d in enumerate(forecast_dates):
        code = daily.get("weather_code", [0])[i] if i < len(daily.get("weather_code", [])) else 0
        lookup[d] = {
            "condition": WEATHER_CODES.get(code, f"Code {code}"),
            "high_temp": daily.get("temperature_2m_max", [0])[i] if i < len(daily.get("temperature_2m_max", [])) else None,
            "low_temp": daily.get("temperature_2m_min", [0])[i] if i < len(daily.get("temperature_2m_min", [])) else None,
            "precipitation": daily.get("precipitation_probability_max", [0])[i] if i < len(daily.get("precipitation_probability_max", [])) else 0,
            "wind": daily.get("wind_speed_10m_max", [0])[i] if i < len(daily.get("wind_speed_10m_max", [])) else 0,
        }

    return lookup


def _get_project_location(project: dict) -> str | None:
    """Extract a geocodable location string from a cached project."""
    addr = project.get("address", {})
    city = addr.get("city", "")
    state = addr.get("state", "")
    zipcode = addr.get("zipcode", "")
    if city:
        parts = [city]
        if state:
            parts.append(state)
        if zipcode:
            parts.append(zipcode)
        return ", ".join(parts)
    return None


async def enrich_dates_with_weather(
    dates: list[str],
    category: str,
    project: dict | None = None,
) -> list[dict[str, Any]] | None:
    """Enrich available dates with weather indicators for outdoor projects.

    Returns a list of enriched date dicts, or ``None`` if weather data
    is unavailable (caller should fall back to plain dates).
    """
    if not is_outdoor_project(category):
        return None

    # Resolve location from project address
    location = _get_project_location(project) if project else None
    if not location:
        logger.info("Weather enrichment skipped: no project address available")
        return None

    forecast_lookup = await _fetch_forecast(location, dates)
    if not forecast_lookup:
        return None

    enriched: list[dict[str, Any]] = []
    for date_str in dates:
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            day_name = date_obj.strftime("%A")
            display_date = date_obj.strftime("%m/%d/%Y")
        except ValueError:
            day_name = date_str
            display_date = date_str

        entry: dict[str, Any] = {
            "date": date_str,
            "display_date": display_date,
            "day_name": day_name,
        }

        forecast = forecast_lookup.get(date_str)
        if forecast:
            assessment = _analyze_suitability(forecast, category)
            entry["condition"] = forecast.get("condition", "Unknown")
            entry["high_temp"] = forecast.get("high_temp")
            entry["low_temp"] = forecast.get("low_temp")
            entry["precipitation"] = forecast.get("precipitation", 0)
            entry["wind"] = forecast.get("wind", 0)
            entry["suitable"] = assessment["suitable"]
            entry["severity"] = assessment["severity"]
            entry["warnings"] = assessment["warnings"]
            if assessment["suitable"]:
                entry["indicator"] = "[GOOD]"
            elif assessment["severity"] == "high":
                entry["indicator"] = "[BAD]"
            else:
                entry["indicator"] = "[WARN]"
        else:
            # No forecast for this date (beyond forecast range)
            entry["suitable"] = True
            entry["indicator"] = ""
            entry["warnings"] = []

        enriched.append(entry)

    good = sum(1 for d in enriched if d.get("suitable", True))
    logger.info(
        "Weather enrichment: %d/%d dates suitable for %s in %s",
        good, len(enriched), category, location,
    )
    return enriched
