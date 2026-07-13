import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_FILE = CACHE_DIR / "weather_feed.json"

# Create directories if they do not exist
os.makedirs(CACHE_DIR, exist_ok=True)

# Geolocation Coordinates for Weather API (e.g., Bangalore, India)
LATITUDE = 12.9716
LONGITUDE = 77.5946
TIMEZONE = "auto"

# Weather Simulation Fallback Bounds (for mathematical generator)
TEMP_MIN = 15.0  # °C
TEMP_MAX = 35.0  # °C
WIND_MIN = 0.0   # m/s
WIND_MAX = 15.0  # m/s

# Solar PV Parameters
SOLAR_EFFICIENCY = 0.18    # eta (18% efficiency)
SOLAR_AREA = 500.0         # A (500 sq meters of panels)
TEMP_COEFF = 0.004         # beta (0.4% per °C temperature coefficient)
REF_TEMP = 25.0            # T_ref (°C)
RADIATION_HEAT_COEFF = 0.03  # gamma (°C per W/m2 heat absorption coefficient)

# Wind Turbine Parameters
WIND_CUT_IN = 3.0          # v_cut-in (m/s)
WIND_RATED = 12.0          # v_rated (m/s)
WIND_CUT_OUT = 25.0        # v_cut-out (m/s)
AIR_DENSITY = 1.225        # rho (kg/m3)
ROTOR_AREA = 1250.0        # A (sq meters, based on ~40m rotor diameter)
POWER_COEFF = 0.4          # Cp (aerodynamic power coefficient)
WIND_RATED_POWER = 1500.0  # P_rated (kW)

# Battery Parameters
BATTERY_CAPACITY_KWH = 2000.0  # Capacity of battery (kWh)
BATTERY_MAX_CHARGE_KW = 500.0  # Max charging rate (kW)
BATTERY_MAX_DISCHARGE_KW = 500.0  # Max discharging rate (kW)
BATTERY_EFFICIENCY = 0.95      # Charge/discharge efficiency (eta_c = eta_d = 0.95, round-trip ~90%)
BATTERY_MIN_SOC = 10.0         # Minimum State of Charge (%)
BATTERY_MAX_SOC = 90.0         # Maximum State of Charge (%)
BATTERY_DEGRADATION_PER_CYCLE = 0.0001  # Max capacity loss fraction per full equivalent cycle

# Campus Building Meter Configurations
BUILDINGS_CONFIG = {
    "Academic_1": {"peak_kw": 150.0, "type": "academic", "power_factor": 0.92},
    "Academic_2": {"peak_kw": 120.0, "type": "academic", "power_factor": 0.92},
    "Lab_1": {"peak_kw": 250.0, "type": "industrial", "power_factor": 0.85},  # lower PF due to motor loads
    "Residential_1": {"peak_kw": 80.0, "type": "residential", "power_factor": 0.95},
    "Residential_2": {"peak_kw": 90.0, "type": "residential", "power_factor": 0.95},
    "Dining_Hall": {"peak_kw": 110.0, "type": "commercial", "power_factor": 0.90},
    "Library": {"peak_kw": 70.0, "type": "academic", "power_factor": 0.94},
    "Gym": {"peak_kw": 60.0, "type": "commercial", "power_factor": 0.88},
    "EV_Station_1": {"peak_kw": 100.0, "type": "ev", "power_factor": 0.97},
    "EV_Station_2": {"peak_kw": 80.0, "type": "ev", "power_factor": 0.97}
}

# Market Parameters
GRID_FREQUENCY_NOMINAL = 50.0  # Hz
GRID_PRICE_BUY_BASE = 5.0      # Base buy price (e.g. INR per kWh)
GRID_PRICE_SELL_BASE = 3.5     # Base sell price (e.g. INR per kWh)



