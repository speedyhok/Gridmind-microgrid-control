import os
import shutil
import pytest
from datetime import datetime
from pydantic import ValidationError

from src.streaming.schemas import SolarTelemetrySchema
from src.streaming.writer import DataLakeWriter
from src.streaming.validator import TelemetryValidatorEngine

TEST_LAKE = "data/test_lake"

@pytest.fixture(autouse=True)
def cleanup_test_lake():
    # Clean before and after test
    if os.path.exists(TEST_LAKE):
        shutil.rmtree(TEST_LAKE)
    yield
    if os.path.exists(TEST_LAKE):
        shutil.rmtree(TEST_LAKE)

def test_pydantic_schema_validation():
    # Valid schema payload
    valid_payload = {
        "timestamp": "2026-07-13T12:00:00",
        "temperature_c": 25.5,
        "irradiance_wm2": 800.0,
        "solar_power_kw": 60.5,
        "weather_source": "real_api"
    }
    model = SolarTelemetrySchema(**valid_payload)
    assert model.temperature_c == 25.5

    # Invalid payload (temperature exceeds maximum limit of 60.0°C)
    invalid_payload = valid_payload.copy()
    invalid_payload["temperature_c"] = 75.0  # ge=-10, le=60 in schema
    with pytest.raises(ValidationError):
        SolarTelemetrySchema(**invalid_payload)

def test_outlier_spikes_alert():
    engine = TelemetryValidatorEngine(lake_root=TEST_LAKE)
    
    # Battery cell temperature at normal (25°C) -> no anomaly
    payload_ok = {
        "timestamp": "2026-07-13T12:00:00",
        "battery_power_kw": 100.0,
        "battery_soc": 50.0,
        "battery_soh": 99.0,
        "battery_cell_temp": 25.0,
        "battery_voltage": 395.0,
        "anomaly_flag": False
    }
    res_ok = engine.detect_anomalies_and_validate("gridmind.telemetry.battery", payload_ok)
    assert res_ok["anomaly_flag"] is False

    # Battery cell temperature at 65°C -> should trigger outlier alert and set anomaly_flag = True
    payload_bad = payload_ok.copy()
    payload_bad["battery_cell_temp"] = 65.0
    res_bad = engine.detect_anomalies_and_validate("gridmind.telemetry.battery", payload_bad)
    assert res_bad["anomaly_flag"] is True

def test_frozen_sensor_flatline():
    engine = TelemetryValidatorEngine(lake_root=TEST_LAKE, window_size=5)
    
    payload = {
        "timestamp": "2026-07-13T12:00:00",
        "wind_speed_ms": 5.0,
        "wind_power_kw": 30.0,
        "wind_vibration": 1.02
    }

    # Stream 4 identical wind turbine readings (should be normal)
    for i in range(4):
        p = payload.copy()
        p["timestamp"] = f"2026-07-13T12:0{i}:00"
        res = engine.detect_anomalies_and_validate("gridmind.telemetry.wind", p)
        assert res["anomaly_flag"] is False

    # 5th identical reading should trigger flatline alert
    p = payload.copy()
    p["timestamp"] = "2026-07-13T12:05:00"
    res = engine.detect_anomalies_and_validate("gridmind.telemetry.wind", p)
    assert res["anomaly_flag"] is True

def test_missing_values_imputation():
    engine = TelemetryValidatorEngine(lake_root=TEST_LAKE)
    
    # Send some valid entries to seed history
    for i in range(3):
        payload = {
            "timestamp": f"2026-07-13T12:0{i}:00",
            "electricity_buy_price_inr": float(10.0 + i), # 10.0, 11.0, 12.0 (avg = 11.0)
            "electricity_sell_price_inr": 7.0,
            "grid_frequency_hz": 50.0
        }
        engine.detect_anomalies_and_validate("gridmind.telemetry.market", payload)

    # Send an entry with missing buy price
    broken_payload = {
        "timestamp": "2026-07-13T12:03:00",
        "electricity_sell_price_inr": 7.0,
        "grid_frequency_hz": 50.0
    }
    
    # Validator should impute missing field to 11.0
    res = engine.detect_anomalies_and_validate("gridmind.telemetry.market", broken_payload)
    assert res["electricity_buy_price_inr"] == 11.0

def test_parquet_file_writing():
    writer = DataLakeWriter(root_path=TEST_LAKE)
    record = {
        "timestamp": "2026-07-13T12:00:00",
        "temperature_c": 25.5,
        "irradiance_wm2": 800.0,
        "solar_power_kw": 60.5,
        "weather_source": "real_api"
    }
    file_path = writer.write_record("gridmind.telemetry.solar", record)
    
    assert os.path.exists(file_path)
    assert file_path.endswith(".parquet")
    # Verify file directory layout
    assert "year=2026" in file_path
    assert "month=07" in file_path
    assert "day=13" in file_path
    assert "hour=12" in file_path
