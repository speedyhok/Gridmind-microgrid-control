import os
import shutil
import pytest
from datetime import datetime, timedelta

from src.storage.db_client import GridMindDBClient
from src.storage.db_writer import GridMindTelemetryWriter
from src.analytics.forecaster import GridMindForecaster
from src.simulator.weather import WeatherSimulator

TEST_DB_PATH = "data/gridmind_storage_test.db"
TEST_REPORT_PATH = "data/test_ml_report.md"

@pytest.fixture(autouse=True)
def setup_mock_db():
    # Cleanup before
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass
    if os.path.exists(TEST_REPORT_PATH):
        try:
            os.remove(TEST_REPORT_PATH)
        except PermissionError:
            pass

    # Establish mock database and populate with at least 15 chronological records to allow model fitting
    client = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    writer = GridMindTelemetryWriter(db_client=client)

    base_time = datetime.now() - timedelta(hours=30)
    for i in range(25):
        time_str = (base_time + timedelta(hours=i)).isoformat()
        
        # 1. Market prices
        writer.write_pricing_record(time_str, 10.0 + (i % 3), 7.0 + (i % 2), 50.0 + 0.01 * (i % 5))
        # 2. Solar
        writer.write_power_record(time_str, "solar_pv", 20.0 + i, None, "operational", False)
        # 3. Wind
        writer.write_power_record(time_str, "wind_turbine", 100.0 + i, None, "operational", False)
        # 4. Battery
        writer.write_power_record(time_str, "battery_bank", 50.0, 398.0, "operational", False)
        # 5. Meters aggregate
        writer.write_power_record(time_str, "campus_aggregate", 500.0 + 10 * i, None, "operational", False)

    writer.close()
    
    yield
    
    # Cleanup after
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass
    if os.path.exists(TEST_REPORT_PATH):
        try:
            os.remove(TEST_REPORT_PATH)
        except PermissionError:
            pass

@pytest.fixture
def db_conn():
    db = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    yield db
    db.close()

def test_feature_engineering_and_training(db_conn):
    forecaster = GridMindForecaster(db_client=db_conn)
    
    # 1. Load historical and verify shape
    df_raw = forecaster.store.load_historical_data()
    assert len(df_raw) == 25
    assert "buy_price_inr" in df_raw.columns
    assert "solar_power_kw" in df_raw.columns
    assert "demand_power_kw" in df_raw.columns
    assert "temperature_c" in df_raw.columns

    # 2. Build features and verify columns
    df_feat = forecaster.store.build_features(df_raw)
    assert "hour_sin" in df_feat.columns
    assert "wind_speed_cubed" in df_feat.columns
    assert "demand_power_kw_lag_1h" in df_feat.columns
    assert "demand_power_kw_roll_4h" in df_feat.columns

    # 3. Train models
    metrics = forecaster.train_models()
    assert "demand" in metrics
    assert "solar" in metrics
    assert "wind" in metrics
    assert "price" in metrics
    
    assert metrics["demand"]["dataset_size"] == 25
    assert metrics["demand"]["train_samples"] == 20
    assert metrics["demand"]["test_samples"] == 5

    # 4. Verification report file created
    forecaster.save_performance_report(report_path=TEST_REPORT_PATH)
    assert os.path.exists(TEST_REPORT_PATH)

def test_forecasting_inference_and_clamping(db_conn):
    forecaster = GridMindForecaster(db_client=db_conn)
    
    # Train models
    forecaster.train_models()

    # Generate 24 hour prediction list
    forecast = forecaster.generate_24h_forecast()
    
    # Verify predictions returned
    assert len(forecast) == 24
    
    # Check structure
    assert "time" in forecast[0]
    assert "demand" in forecast[0]
    assert "solar" in forecast[0]
    assert "wind" in forecast[0]
    assert "price" in forecast[0]

    # Verify predictions written to database predictions table
    pred_rows = db_conn.fetch_all("SELECT count(*) FROM predictions;")
    # 24 hours * 4 targets = 96 rows
    assert pred_rows[0][0] == 96

    # Verify that pricing predictions are saved
    price_preds = db_conn.fetch_all("SELECT predicted_value FROM predictions WHERE target_type='price';")
    assert len(price_preds) == 24
    for val in price_preds:
        assert val[0] >= 0.5  # clamp price limit check
