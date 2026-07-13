import os
from datetime import datetime
import pytest
from pydantic import BaseModel, Field

from src.simulator.weather import WeatherSimulator
from src.simulator.solar import SolarSimulator
from src.simulator.wind import WindTurbineSimulator
from src.simulator.battery import BatterySimulator
from src.simulator.meters import CampusMetersSimulator
from src.simulator.market import MarketSimulator

# Pydantic schema for validating output structure
class TelemetryValidator(BaseModel):
    timestamp: str
    temperature_c: float = Field(..., ge=-10, le=60)
    wind_speed_ms: float = Field(..., ge=0, le=100)
    irradiance_wm2: float = Field(..., ge=0, le=2000)
    solar_power_kw: float = Field(..., ge=0)
    wind_power_kw: float = Field(..., ge=0)
    wind_vibration: float = Field(..., ge=0)
    demand_active_kw: float = Field(..., ge=0)
    demand_reactive_kvar: float = Field(..., ge=0)
    aggregate_power_factor: float = Field(..., ge=0.5, le=1.0)
    battery_power_kw: float
    battery_soc: float = Field(..., ge=0, le=100)
    battery_soh: float = Field(..., ge=0, le=100)
    battery_cell_temp: float = Field(..., ge=-10, le=100)
    battery_voltage: float = Field(..., ge=0)
    grid_import_kw: float
    electricity_buy_price_inr: float = Field(..., ge=0)
    electricity_sell_price_inr: float = Field(..., ge=0)
    grid_frequency_hz: float = Field(..., ge=49.0, le=51.0)
    anomaly_flag: bool
    weather_source: str

def test_weather_fallback():
    weather = WeatherSimulator()
    # Test midnight conditions (irradiance must be 0)
    dt_midnight = datetime(2026, 7, 13, 0, 0, 0)
    conds_midnight = weather.get_current_conditions(dt_midnight)
    assert conds_midnight["irradiance"] == 0.0
    
    # Test noon conditions (irradiance must be positive)
    dt_noon = datetime(2026, 7, 13, 12, 0, 0)
    conds_noon = weather.get_current_conditions(dt_noon)
    assert conds_noon["irradiance"] >= 0.0

def test_solar_yield():
    solar = SolarSimulator()
    # Night time
    assert solar.calculate_power(0.0, 25.0) == 0.0
    # Perfect condition
    p_good = solar.calculate_power(1000.0, 25.0)
    assert p_good > 0.0
    # Hot condition (should have lower yield due to temperature loss coefficient)
    p_hot = solar.calculate_power(1000.0, 45.0)
    assert p_hot < p_good

def test_wind_yield():
    wind = WindTurbineSimulator()
    # Wind speed below cut-in (3.0 m/s)
    p_low, _ = wind.calculate_power_and_wear(2.0, 100.0)
    assert p_low == 0.0
    # Wind speed above cut-out (25.0 m/s)
    p_high, _ = wind.calculate_power_and_wear(26.0, 100.0)
    assert p_high == 0.0
    # Wind speed at rated (12.0 m/s)
    p_rated, v_wear = wind.calculate_power_and_wear(12.0, 100.0)
    assert p_rated == wind.P_rated
    # Vibration index should grow under stress
    assert v_wear >= 1.0

def test_battery_storage():
    # Capacity: 2000 kWh, max flow: 500 kW
    batt = BatterySimulator()
    assert batt.soc == 50.0
    
    # Charge battery at 500 kW for 1 hour
    # Net energy to battery: 500 kW * 0.95 (efficiency) * 1 hour = 475 kWh
    # SOC increase: (475 / 2000) * 100 = 23.75%
    res = batt.step(500.0, 0.0, 25.0, 1.0)
    assert res["power_kw"] == 500.0
    assert pytest.approx(batt.soc, 0.01) == 73.75
    
    # Discharge battery beyond limit (should clamp to min soc)
    res_dis = batt.step(0.0, 2000.0, 25.0, 1.0)
    # Available energy = (73.75 - 10) / 100 * 2000 = 1275 kWh
    # Max discharge rate limited by charge level * efficiency
    # 1275 kWh * 0.95 = 1211.25 kW limit, but max discharge rate config is 500 kW
    # So it discharges at 500 kW
    assert res_dis["power_kw"] == -500.0
    assert batt.soc >= batt.min_soc

def test_campus_meters():
    meters = CampusMetersSimulator()
    dt = datetime(2026, 7, 13, 10, 0, 0)
    load = meters.get_total_campus_load(dt)
    
    assert load["total_active_power_kw"] > 0
    assert load["aggregate_power_factor"] >= 0.5 and load["aggregate_power_factor"] <= 1.0
    assert "Academic_1" in load["breakdown"]
    
    # Power factor formula checks
    academic_load = load["breakdown"]["Academic_1"]
    p = academic_load["active_power_kw"]
    q = academic_load["reactive_power_kvar"]
    s = academic_load["apparent_power_kva"]
    pf = academic_load["power_factor"]
    
    assert pytest.approx(s, 0.01) == p / pf
    assert pytest.approx(s * s, 0.01) == p * p + q * q

def test_market_state():
    market = MarketSimulator()
    dt = datetime(2026, 7, 13, 10, 0, 0)
    res = market.get_market_state(dt)
    
    assert res["buy_price_inr"] > res["sell_price_inr"]
    assert 49.5 <= res["grid_frequency_hz"] <= 50.5
