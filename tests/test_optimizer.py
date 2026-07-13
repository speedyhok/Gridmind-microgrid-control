import os
import pytest
from datetime import datetime, timedelta

from src.storage.db_client import GridMindDBClient
from src.storage.db_writer import GridMindTelemetryWriter
from src.analytics.optimizer import GridMindOptimizer

TEST_DB_PATH = "data/gridmind_storage_test.db"

@pytest.fixture(autouse=True)
def setup_mock_predictions():
    # Cleanup before
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass

    client = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    
    # Insert mock 24h predictions
    base_time = datetime.now()
    created_at_str = datetime.now().isoformat()
    
    rows_to_insert = []
    # Generate 24 hours of mock predictions
    for h in range(24):
        time_iso = (base_time + timedelta(hours=h)).isoformat()
        
        # Make electricity buy price highly variable to test cost shifting:
        # Hour 0-5: cheap (5 INR) -> battery should charge
        # Hour 17-21: expensive (15 INR) -> battery should discharge
        price_val = 5.0 if (0 <= h <= 5) else (15.0 if (17 <= h <= 21) else 10.0)
        
        rows_to_insert.append((time_iso, "price", price_val, created_at_str))
        rows_to_insert.append((time_iso, "demand", 500.0, created_at_str))
        rows_to_insert.append((time_iso, "solar", 100.0 if (8 <= h <= 16) else 0.0, created_at_str))
        rows_to_insert.append((time_iso, "wind", 50.0, created_at_str))

    insert_query = """
    INSERT INTO predictions (time, target_type, predicted_value, created_at)
    VALUES (%s, %s, %s, %s);
    """
    client.executemany(insert_query, rows_to_insert)
    client.close()

    yield

    # Cleanup after
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass

def test_optimizer_logical_constraints():
    db = GridMindDBClient(pg_port=9999, sqlite_path=TEST_DB_PATH)
    optimizer = GridMindOptimizer(db)
    
    try:
        # Run optimization starting at 50% SOC (1000 kWh)
        schedule = optimizer.optimize_schedule(initial_soc=1000.0)
        
        assert len(schedule) == 24
        
        for idx, row in enumerate(schedule):
            charge = row["charge_kw"]
            discharge = row["discharge_kw"]
            soc = row["battery_soc_kwh"]
            buy = row["grid_buy_kw"]
            sell = row["grid_sell_kw"]
            demand = row["demand_kw"]
            solar = row["solar_kw"]
            wind = row["wind_kw"]
            
            # 1. Simultaneous charge and discharge interlock check
            assert not (charge > 0.1 and discharge > 0.1), f"Hour {idx} charging and discharging simultaneously!"
            
            # 2. SoC capacity bounds check (200 to 1800 kWh)
            assert 200.0 <= soc <= 1800.0, f"Hour {idx} SoC {soc} out of limits!"
            
            # 3. Energy balance equation verification
            # grid_buy + solar + wind + discharge == demand + charge + grid_sell
            lhs = round(buy + solar + wind + discharge, 1)
            rhs = round(demand + charge + sell, 1)
            assert lhs == rhs, f"Hour {idx} energy balance violated! LHS: {lhs}, RHS: {rhs}"
            
        # 4. Check that optimizer charged the battery during cheap hours
        # Cheap hours are index 0-5 (price = 5 INR)
        cheap_charging = [schedule[h]["charge_kw"] for h in range(6)]
        assert sum(cheap_charging) > 0.0, "Optimizer failed to charge the battery during cheap pricing hours!"
        
        # Expensive hours are index 17-21 (price = 15 INR)
        expensive_discharging = [schedule[h]["discharge_kw"] for h in range(17, 22)]
        assert sum(expensive_discharging) > 0.0, "Optimizer failed to discharge the battery during expensive pricing hours!"

    finally:
        db.close()
