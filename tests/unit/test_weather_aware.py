"""Tests for weather-aware scheduling."""

import pytest

from tools.weather_aware import (
    _analyze_suitability,
    _get_category_criteria,
    is_outdoor_project,
)


class TestIsOutdoorProject:
    def test_roofing(self):
        assert is_outdoor_project("Roofing") is True

    def test_decking(self):
        assert is_outdoor_project("Decking") is True

    def test_windows(self):
        assert is_outdoor_project("Windows") is True

    def test_doors(self):
        assert is_outdoor_project("Doors") is True

    def test_fencing(self):
        assert is_outdoor_project("Fencing") is True

    def test_exterior_painting(self):
        assert is_outdoor_project("Exterior Painting") is True

    def test_concrete(self):
        assert is_outdoor_project("Concrete") is True

    def test_grill(self):
        assert is_outdoor_project("Grill") is True

    def test_balcony(self):
        assert is_outdoor_project("Balcony") is True

    def test_balcony_grill_installation(self):
        assert is_outdoor_project("Balcony grill Installation") is True

    def test_patio(self):
        assert is_outdoor_project("Patio") is True

    def test_solar(self):
        assert is_outdoor_project("Solar") is True

    def test_gutter(self):
        assert is_outdoor_project("Gutter") is True

    def test_case_insensitive(self):
        assert is_outdoor_project("ROOFING") is True
        assert is_outdoor_project("roofing repair") is True

    def test_substring_match(self):
        assert is_outdoor_project("Fence Installation") is True

    def test_project_type_fallback(self):
        """Category empty but projectType matches."""
        assert is_outdoor_project("", "Windows Installation") is True

    def test_project_type_only(self):
        assert is_outdoor_project("", "Balcony grill Installation") is True

    def test_unknown_defaults_outdoor(self):
        """Unknown category/type defaults to outdoor (most field work is)."""
        assert is_outdoor_project("Custom Work") is True

    def test_indoor_excluded(self):
        assert is_outdoor_project("Plumbing") is False
        assert is_outdoor_project("Electrical") is False
        assert is_outdoor_project("HVAC") is False
        assert is_outdoor_project("Cabinet Installation") is False
        assert is_outdoor_project("Interior Painting") is False

    def test_empty(self):
        assert is_outdoor_project("") is False

    def test_none(self):
        assert is_outdoor_project(None) is False


class TestGetCategoryCriteria:
    def test_roofing_stricter(self):
        c = _get_category_criteria("Roofing")
        assert c["wind_max"] == 20
        assert c["rain_threshold"] == 30

    def test_fencing_lenient(self):
        c = _get_category_criteria("Fencing")
        assert c["wind_max"] == 30
        assert c["temp_min"] == 32

    def test_exterior_painting_strictest_rain(self):
        c = _get_category_criteria("Exterior Painting")
        assert c["rain_threshold"] == 20
        assert c["wind_max"] == 15

    def test_unknown_returns_default(self):
        c = _get_category_criteria("Unknown Category")
        assert c["rain_threshold"] == 50
        assert c["wind_max"] == 25


class TestAnalyzeSuitability:
    def test_good_weather(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 10,
            "wind": 8,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["suitable"] is True
        assert result["warnings"] == []
        assert result["severity"] == "low"

    def test_rain_warning(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 60,
            "wind": 8,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["suitable"] is False
        assert any("precipitation" in w for w in result["warnings"])

    def test_high_precip_is_high_severity(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 75,
            "wind": 8,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["severity"] == "high"

    def test_thunderstorm_is_high_severity(self):
        forecast = {
            "condition": "Thunderstorm",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 80,
            "wind": 8,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["severity"] == "high"
        assert any("Thunderstorm" in w for w in result["warnings"])

    def test_cold_temperature_warning(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 30,
            "low_temp": 20,
            "precipitation": 0,
            "wind": 5,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["suitable"] is False
        assert any("cold" in w for w in result["warnings"])

    def test_hot_temperature_warning(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 105,
            "low_temp": 98,
            "precipitation": 0,
            "wind": 5,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["suitable"] is False
        assert any("hot" in w for w in result["warnings"])

    def test_wind_warning(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 0,
            "wind": 25,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["suitable"] is False
        assert any("wind" in w.lower() for w in result["warnings"])

    def test_extreme_wind_is_high_severity(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 0,
            "wind": 35,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert result["severity"] == "high"

    def test_painting_strictest_rain_threshold(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 25,
            "wind": 5,
        }
        result = _analyze_suitability(forecast, "Exterior Painting")
        assert result["suitable"] is False
        assert any("precipitation" in w for w in result["warnings"])

    def test_fencing_tolerates_same_precip(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 25,
            "wind": 5,
        }
        result = _analyze_suitability(forecast, "Fencing")
        assert result["suitable"] is True

    def test_no_forecast_returns_suitable(self):
        result = _analyze_suitability({}, "Roofing")
        assert result["suitable"] is True

    def test_recommendation_high_severity(self):
        forecast = {
            "condition": "Snow",
            "high_temp": 25,
            "low_temp": 15,
            "precipitation": 80,
            "wind": 30,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert "strongly recommend" in result["recommendation"].lower()

    def test_recommendation_good_weather(self):
        forecast = {
            "condition": "Clear sky",
            "high_temp": 72,
            "low_temp": 55,
            "precipitation": 5,
            "wind": 5,
        }
        result = _analyze_suitability(forecast, "Roofing")
        assert "look good" in result["recommendation"].lower()
