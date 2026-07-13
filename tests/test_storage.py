import os
import pytest
from src.storage.db_client import GridMindDBClient
from src.storage.db_writer import GridMindTelemetryWriter

TEST_DB_PATH = "data/gridmind_storage_test.db"

@pytest.fixture(autouse=True)
def cleanup_test_db():
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass
    yield
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass

def test_database_client_fallback_and_schema():
    # Force connection error in Postgres by supplying invalid port
    client = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    try:
        assert client.mode == "sqlite"
        assert os.path.exists(TEST_DB_PATH)
        
        # Query tables in SQLite to verify schema creation
        tables = client.fetch_all("SELECT name FROM sqlite_master WHERE type='table';")
        table_names = [t[0] for t in tables]
        assert "assets" in table_names
        assert "electricity_prices" in table_names
        assert "telemetry_power" in table_names
    finally:
        client.close()

def test_telemetry_writer_seeding_and_ingestion():
    client = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    writer = GridMindTelemetryWriter(db_client=client)
    try:
        # 1. Seeding Asset checks
        assets = client.fetch_all("SELECT asset_id, asset_type, capacity_kw FROM assets;")
        asset_ids = [a[0] for a in assets]
        assert "solar_pv" in asset_ids
        assert "wind_turbine" in asset_ids
        assert "battery_bank" in asset_ids
        assert "Academic_1" in asset_ids
        
        # 2. Ingest Market pricing check
        market_payload = {
            "timestamp": "2026-07-13T12:00:00",
            "electricity_buy_price_inr": 10.5,
            "electricity_sell_price_inr": 7.2,
            "grid_frequency_hz": 50.02
        }
        writer.ingest_payload("gridmind.telemetry.market", market_payload)
        
        prices = client.fetch_all("SELECT * FROM electricity_prices;")
        assert len(prices) == 1
        assert prices[0][1] == 10.5
        assert prices[0][3] == 50.02

        # 3. Ingest Solar checks
        solar_payload = {
            "timestamp": "2026-07-13T12:00:00",
            "temperature_c": 25.0,
            "irradiance_wm2": 800.0,
            "solar_power_kw": 60.0,
            "weather_source": "real_api",
            "anomaly_flag": False
        }
        writer.ingest_payload("gridmind.telemetry.solar", solar_payload)

        # 4. Ingest building smart meters check
        meter_payload = {
            "timestamp": "2026-07-13T12:00:00",
            "demand_active_kw": 800.0,
            "demand_reactive_kvar": 300.0,
            "aggregate_power_factor": 0.93,
            "anomaly_flag": False,
            "breakdown": {
                "Academic_1": {"active_power_kw": 120.0},
                "Lab_1": {"active_power_kw": 200.0}
            }
        }
        writer.ingest_payload("gridmind.telemetry.meters", meter_payload)

        # Fetch total telemetry entries
        telemetry = client.fetch_all("SELECT asset_id, power_kw FROM telemetry_power;")
        telemetry_map = {t[0]: t[1] for t in telemetry}
        
        assert "solar_pv" in telemetry_map
        assert telemetry_map["solar_pv"] == 60.0
        assert "campus_aggregate" in telemetry_map
        assert telemetry_map["campus_aggregate"] == 800.0
        assert "Academic_1" in telemetry_map
        assert telemetry_map["Academic_1"] == 120.0
        assert "Lab_1" in telemetry_map
        assert telemetry_map["Lab_1"] == 200.0
    finally:
        writer.close()
