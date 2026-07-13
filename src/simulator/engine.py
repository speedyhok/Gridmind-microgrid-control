"""
Shared simulation tick engine for GridMind.
Used by both run_sim.py (batch mode) and the API live-update endpoint (single-step mode).
Reads data/control_overrides.json on every tick to honour manual battery overrides.
"""
import os
import json
import logging
from datetime import datetime

from src.simulator.weather import WeatherSimulator
from src.simulator.solar import SolarSimulator
from src.simulator.wind import WindTurbineSimulator
from src.simulator.battery import BatterySimulator
from src.simulator.meters import CampusMetersSimulator
from src.simulator.market import MarketSimulator
from src.simulator.config import LATITUDE, LONGITUDE

logger = logging.getLogger(__name__)

OVERRIDE_PATH = "data/control_overrides.json"

def read_override() -> dict:
    """
    Reads the manual battery override file if it exists.
    Returns a dict like {"command": "charge", "rate_kw": 350.0} or {"command": "none"}.
    """
    if not os.path.exists(OVERRIDE_PATH):
        return {"command": "none", "rate_kw": 0.0}
    try:
        with open(OVERRIDE_PATH, "r") as f:
            data = json.load(f)
        return data
    except Exception as e:
        logger.warning(f"Could not read override file: {e}")
        return {"command": "none", "rate_kw": 0.0}


def run_single_tick(
    step_time: datetime,
    battery_sim: BatterySimulator,
    weather_sim: WeatherSimulator = None,
    solar_sim: SolarSimulator = None,
    wind_sim: WindTurbineSimulator = None,
    meters_sim: CampusMetersSimulator = None,
    market_sim: MarketSimulator = None,
    dt_hours: float = 1.0,
    lat: float = LATITUDE,
    lon: float = LONGITUDE,
) -> dict:
    """
    Runs a single simulation tick at step_time.
    Reads the override file to apply any manual battery command overrides.
    Returns the full telemetry record dict.
    """
    # Lazily initialize simulators if not passed in (for API single-step use)
    weather_sim = weather_sim or WeatherSimulator(lat=lat, lon=lon)
    solar_sim = solar_sim or SolarSimulator()
    wind_sim = wind_sim or WindTurbineSimulator()
    meters_sim = meters_sim or CampusMetersSimulator()
    market_sim = market_sim or MarketSimulator()

    timestamp_sec = step_time.timestamp()

    # 1. Weather
    weather = weather_sim.get_current_conditions(step_time)
    temp = weather["temperature"]
    wind_speed = weather["wind_speed"]
    irradiance = weather["irradiance"]

    # 2. Renewables
    solar_kw = solar_sim.calculate_power(irradiance, temp)
    wind_kw, wind_wear = wind_sim.calculate_power_and_wear(wind_speed, timestamp_sec)

    # 3. Campus Load
    load = meters_sim.get_total_campus_load(step_time)
    demand_kw = load["total_active_power_kw"]
    reactive_kvar = load["total_reactive_power_kvar"]
    agg_pf = load["aggregate_power_factor"]

    # 4. Market Pricing
    market = market_sim.get_market_state(step_time)
    buy_price = market["buy_price_inr"]
    sell_price = market["sell_price_inr"]
    freq = market["grid_frequency_hz"]

    # 5. Read override file and determine battery dispatch
    override = read_override()
    net_generation = solar_kw + wind_kw
    deficit = demand_kw - net_generation

    if override["command"] == "charge":
        # Force charge at specified rate, ignoring surplus/deficit dispatch logic
        charge_demand = float(override.get("rate_kw", 0.0))
        discharge_demand = 0.0
        logger.info(f"[OVERRIDE] Forced charging at {charge_demand:.1f} kW")
    elif override["command"] == "discharge":
        # Force discharge at specified rate
        charge_demand = 0.0
        discharge_demand = float(override.get("rate_kw", 0.0))
        logger.info(f"[OVERRIDE] Forced discharging at {discharge_demand:.1f} kW")
    else:
        # Normal optimizer dispatch: surplus charges, deficit discharges
        if deficit < 0:
            charge_demand = abs(deficit)
            discharge_demand = 0.0
        else:
            charge_demand = 0.0
            discharge_demand = deficit

    # 6. Step battery model
    battery_state = battery_sim.step(
        charge_demand_kw=charge_demand,
        discharge_demand_kw=discharge_demand,
        ambient_temp=temp,
        dt_hours=dt_hours,
    )

    battery_power = battery_state["power_kw"]
    soc = battery_state["soc"]
    soh = battery_state["soh"]
    cell_temp = battery_state["cell_temp"]
    anomaly = battery_state["anomaly_flag"]

    # 7. Final grid import/export
    grid_import_kw = deficit + battery_power

    record = {
        "timestamp": step_time.isoformat(),
        "temperature_c": temp,
        "wind_speed_ms": wind_speed,
        "irradiance_wm2": irradiance,
        "solar_power_kw": solar_kw,
        "wind_power_kw": wind_kw,
        "wind_vibration": wind_wear,
        "demand_active_kw": demand_kw,
        "demand_reactive_kvar": reactive_kvar,
        "aggregate_power_factor": agg_pf,
        "battery_power_kw": battery_power,
        "battery_soc": soc,
        "battery_soh": soh,
        "battery_cell_temp": cell_temp,
        "battery_voltage": battery_state["voltage"],
        "grid_import_kw": round(grid_import_kw, 3),
        "electricity_buy_price_inr": buy_price,
        "electricity_sell_price_inr": sell_price,
        "grid_frequency_hz": freq,
        "anomaly_flag": anomaly,
        "weather_source": weather["source"],
        "override_active": override["command"] != "none",
        "override_command": override["command"],
        "override_rate_kw": float(override.get("rate_kw", 0.0)),
        "load_breakdown": load.get("breakdown", {}),
    }

    return record
