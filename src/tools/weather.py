"""Weather tool handler — Open-Meteo API for forecasts."""

import json
import logging
import re
from datetime import datetime
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}


async def _geocode(location: str) -> dict | None:
    """Geocode a location using Open-Meteo Geocoding API."""
    # Strategy 1: ZIP code
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", location)
    if zip_match:
        zip_code = zip_match.group(1)
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={zip_code}&count=5&language=en&format=json"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url)
                res.raise_for_status()
                data = res.json()
                if data.get("results"):
                    for r in data["results"]:
                        if r.get("country") == "United States":
                            return {
                                "latitude": r["latitude"],
                                "longitude": r["longitude"],
                                "name": r["name"],
                                "admin1": r.get("admin1", ""),
                            }
        except Exception:
            logger.warning("ZIP geocoding failed for %s", zip_code)

    # Strategy 2: Extract state abbreviation
    state_name = None
    for abbr, full_name in US_STATES.items():
        if re.search(rf"(?:^|[,\s])({abbr})(?:[,\s\-]|$)", location, re.IGNORECASE):
            state_name = full_name
            break

    # Strategy 3: Clean up city name
    parts = [p.strip() for p in location.split(",")]
    city_name = parts[0] if parts else location
    # Remove ZIP from city
    city_name = re.sub(r"\s*\d{5}(-\d{4})?\s*", "", city_name).strip()
    # Remove state abbr
    for abbr in US_STATES:
        city_name = re.sub(rf"\b{abbr}\b", "", city_name, flags=re.IGNORECASE).strip()

    if not city_name:
        city_name = location

    search_query = f"{city_name} {state_name}" if state_name else city_name
    encoded = quote(search_query)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded}&count=5&language=en&format=json"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url)
            res.raise_for_status()
            data = res.json()
            results = data.get("results", [])
            if results:
                # Prefer US results
                for r in results:
                    if r.get("country") == "United States":
                        return {
                            "latitude": r["latitude"],
                            "longitude": r["longitude"],
                            "name": r["name"],
                            "admin1": r.get("admin1", ""),
                        }
                # Fallback to first result
                r = results[0]
                return {
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "name": r["name"],
                    "admin1": r.get("admin1", ""),
                }
    except Exception:
        logger.exception("Geocoding failed for: %s", location)

    return None


def _get_project_location() -> str | None:
    """Try to get a location from the project cache (most recent project with an address)."""
    try:
        from auth.context import AuthContext
        from tools.scheduling import _projects_cache

        customer_id = AuthContext.get_customer_id()
        entry = _projects_cache.get(customer_id)
        if not entry:
            return None

        for project in entry["projects"]:
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
                location = ", ".join(parts)
                logger.info("Weather: using project address from cache: %s", location)
                return location
    except Exception:
        logger.debug("Could not resolve project location from cache")
    return None


async def get_weather(location: str = "", target_date: str = "") -> str:
    """Get weather forecast for a location.

    If ``target_date`` (YYYY-MM-DD) is provided, extends the forecast range
    to cover that date and highlights it in the response.  Otherwise returns
    the default 5-day forecast from today.
    """
    if not location or not location.strip():
        project_location = _get_project_location()
        if project_location:
            location = project_location
        else:
            return (
                "I need a location for the weather forecast. "
                "Please provide a city and state, ZIP code, or address."
            )

    geo = await _geocode(location)
    if not geo:
        return f"Sorry, I couldn't find the location '{location}'. Try a city name, state, or ZIP code."

    # Determine how many forecast days we need
    forecast_days = 5
    if target_date:
        try:
            target_dt = datetime.strptime(target_date[:10], "%Y-%m-%d")
            days_ahead = (target_dt - datetime.now()).days + 1
            if days_ahead > 16:
                return (
                    f"The scheduled date ({target_date[:10]}) is more than 16 days away. "
                    "Weather forecasts are only available up to 16 days out. "
                    "Check back closer to the date for an accurate forecast."
                )
            forecast_days = max(5, min(days_ahead + 1, 16))
        except ValueError:
            logger.warning("Invalid target_date: %s", target_date)

    lat, lon = geo["latitude"], geo["longitude"]
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
        f"&timezone=America/New_York&forecast_days={forecast_days}"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url)
            res.raise_for_status()
            data = res.json()
    except Exception:
        logger.exception("Weather API failed")
        return "Sorry, I couldn't fetch the weather forecast right now."

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        return "No forecast data available."

    location_name = f"{geo['name']}, {geo['admin1']}" if geo.get("admin1") else geo["name"]

    # Build structured forecast for frontend rendering
    forecast_days = []
    for i, date_str in enumerate(dates):
        code = daily.get("weather_code", [0])[i] if i < len(daily.get("weather_code", [])) else 0
        high = daily.get("temperature_2m_max", [0])[i] if i < len(daily.get("temperature_2m_max", [])) else None
        low = daily.get("temperature_2m_min", [0])[i] if i < len(daily.get("temperature_2m_min", [])) else None
        precip = daily.get("precipitation_sum", [0])[i] if i < len(daily.get("precipitation_sum", [])) else 0
        wind = daily.get("wind_speed_10m_max", [0])[i] if i < len(daily.get("wind_speed_10m_max", [])) else 0
        condition = WEATHER_CODES.get(code, f"Code {code}")

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            day_name = dt.strftime("%A")
        except ValueError:
            day_name = ""

        forecast_days.append({
            "date": date_str,
            "day_name": day_name,
            "condition": condition,
            "high": high,
            "low": low,
            "precipitation": precip,
            "wind": wind,
        })

    # Current = today's forecast
    today = forecast_days[0] if forecast_days else {}

    result: dict = {
        "location": location_name,
        "current": {
            "temperature": today.get("high"),
            "condition": today.get("condition", ""),
        },
        "forecast": forecast_days,
        "message": f"5-Day Forecast for {location_name}",
    }

    # If a target date was requested, highlight it and trim the forecast
    if target_date:
        target_key = target_date[:10]
        target_forecast = next(
            (d for d in forecast_days if d["date"] == target_key), None
        )
        if target_forecast:
            result["scheduled_date"] = target_forecast
            result["message"] = (
                f"Weather for {location_name} on {target_forecast['day_name']} "
                f"{target_key}: {target_forecast['condition']}, "
                f"high {target_forecast['high']}°F, low {target_forecast['low']}°F"
            )
            # Keep only a few days around the target for context
            result["forecast"] = [
                d for d in forecast_days
                if abs(_days_between(d["date"], target_key)) <= 2
            ]

    return json.dumps(result, indent=2)


def _days_between(date_a: str, date_b: str) -> int:
    """Return the signed difference in days between two YYYY-MM-DD strings."""
    try:
        a = datetime.strptime(date_a[:10], "%Y-%m-%d")
        b = datetime.strptime(date_b[:10], "%Y-%m-%d")
        return (a - b).days
    except ValueError:
        return 999
