import os
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

from src.storage.db_client import GridMindDBClient
from src.storage.db_writer import GridMindTelemetryWriter
import src.api.main as api_main

TEST_DB_PATH = "data/gridmind_storage_test.db"
OVERRIDE_PATH = "data/control_overrides.json"

@pytest.fixture(scope="module")
def setup_api_db():
    # Setup mock DB file and populate
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass

    client = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    writer = GridMindTelemetryWriter(db_client=client)

    # Seed some prices and telemetry
    base_time = datetime.now() - timedelta(hours=10)
    created_at_str = datetime.now().isoformat()
    
    for i in range(12):
        time_str = (base_time + timedelta(hours=i)).isoformat()
        writer.write_pricing_record(time_str, 10.0 + i, 7.0 + i, 50.0)
        writer.write_power_record(time_str, "solar_pv", 50.0, None, "operational", False)
        writer.write_power_record(time_str, "wind_turbine", 150.0, None, "operational", False)
        writer.write_power_record(time_str, "battery_bank", 0.0, 395.0, "operational", False)
        writer.write_power_record(time_str, "campus_aggregate", 600.0, None, "operational", False)
        
        # Write predictions for optimization tests
        writer.db.execute(
            "INSERT INTO predictions (time, target_type, predicted_value, created_at) VALUES (?, ?, ?, ?);",
            (time_str, "price", 10.0, created_at_str)
        )
        writer.db.execute(
            "INSERT INTO predictions (time, target_type, predicted_value, created_at) VALUES (?, ?, ?, ?);",
            (time_str, "demand", 500.0, created_at_str)
        )
        writer.db.execute(
            "INSERT INTO predictions (time, target_type, predicted_value, created_at) VALUES (?, ?, ?, ?);",
            (time_str, "solar", 100.0, created_at_str)
        )
        writer.db.execute(
            "INSERT INTO predictions (time, target_type, predicted_value, created_at) VALUES (?, ?, ?, ?);",
            (time_str, "wind", 50.0, created_at_str)
        )

    writer.close()

    # Override the app's global db_client instance
    api_main.db_client = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    from src.simulator.battery import BatterySimulator
    api_main.battery_sim = BatterySimulator()
    api_main.telemetry_writer = GridMindTelemetryWriter(db_client=api_main.db_client)

    yield

    # Teardown
    if api_main.db_client:
        api_main.db_client.close()
        
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass

    if os.path.exists(OVERRIDE_PATH):
        try:
            os.remove(OVERRIDE_PATH)
        except PermissionError:
            pass

@pytest.fixture
def client(setup_api_db):
    return TestClient(api_main.app)

def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["database_mode"] == "sqlite"

def test_root_redirect_endpoint(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/static/index.html"

def test_api_status(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    res_json = response.json()
    assert "market_pricing" in res_json
    assert "assets" in res_json
    
    # Assert latest values populated
    assert "solar_pv" in res_json["assets"]
    assert res_json["assets"]["solar_pv"]["power_kw"] == 50.0
    assert res_json["assets"]["campus_aggregate"]["power_kw"] == 600.0

def test_api_metrics_historical(client):
    # Historical prices
    response_prices = client.get("/api/metrics/historical?limit=5")
    assert response_prices.status_code == 200
    res_prices = response_prices.json()
    assert len(res_prices["data"]) <= 5
    assert "buy_price_inr" in res_prices["data"][0]

    # Historical solar telemetry
    response_solar = client.get("/api/metrics/historical?asset_id=solar_pv&limit=5")
    assert response_solar.status_code == 200
    res_solar = response_solar.json()
    assert res_solar["asset_id"] == "solar_pv"
    assert "power_kw" in res_solar["data"][0]

def test_api_schedule_optimization(client):
    response = client.get("/api/schedule")
    assert response.status_code == 200
    schedule = response.json()
    assert len(schedule) == 12  # We inserted 12 forecasting steps
    assert "charge_kw" in schedule[0]
    assert "battery_soc_kwh" in schedule[0]

def test_battery_control_override(client):
    # Missing API Key Header (should fail with 401)
    auth_payload = {"command": "charge", "rate_kw": 350.0}
    response_no_auth = client.post("/api/control/override", json=auth_payload)
    assert response_no_auth.status_code == 401

    # Invalid command (with correct auth key)
    bad_payload = {"command": "invalid", "rate_kw": 200.0}
    response_bad = client.post("/api/control/override", json=bad_payload, headers={"X-API-Key": "gridmind_premium_secret_key"})
    assert response_bad.status_code == 400

    # Valid override command (with correct auth key)
    good_payload = {"command": "charge", "rate_kw": 350.0}
    response_good = client.post("/api/control/override", json=good_payload, headers={"X-API-Key": "gridmind_premium_secret_key"})
    assert response_good.status_code == 200
    assert response_good.json()["status"] == "success"
    assert response_good.json()["override"]["command"] == "charge"
    assert response_good.json()["override"]["rate_kw"] == 350.0

    # Check that file data/control_overrides.json was created
    assert os.path.exists(OVERRIDE_PATH)
