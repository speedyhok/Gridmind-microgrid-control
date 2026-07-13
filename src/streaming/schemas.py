from typing import Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

class SolarTelemetrySchema(BaseModel):
    timestamp: datetime
    temperature_c: float = Field(..., ge=-10.0, le=60.0)
    irradiance_wm2: float = Field(..., ge=0.0)
    solar_power_kw: float = Field(..., ge=0.0)
    weather_source: str

class WindTelemetrySchema(BaseModel):
    timestamp: datetime
    wind_speed_ms: float = Field(..., ge=0.0, le=100.0)
    wind_power_kw: float = Field(..., ge=0.0)
    wind_vibration: float = Field(..., ge=0.0)

class BatteryTelemetrySchema(BaseModel):
    timestamp: datetime
    battery_power_kw: float
    battery_soc: float = Field(..., ge=0.0, le=100.0)
    battery_soh: float = Field(..., ge=0.0, le=100.0)
    battery_cell_temp: float = Field(..., ge=-20.0, le=100.0)
    battery_voltage: float = Field(..., ge=0.0)
    anomaly_flag: bool

class MetersTelemetrySchema(BaseModel):
    timestamp: datetime
    demand_active_kw: float = Field(..., ge=0.0)
    demand_reactive_kvar: float = Field(..., ge=0.0)
    aggregate_power_factor: float = Field(..., ge=0.5, le=1.0)
    breakdown: Dict[str, Any]

class MarketTelemetrySchema(BaseModel):
    timestamp: datetime
    electricity_buy_price_inr: float = Field(..., ge=0.0)
    electricity_sell_price_inr: float = Field(..., ge=0.0)
    grid_frequency_hz: float = Field(..., ge=48.0, le=52.0)

# Dictionary mapping topics to their corresponding schemas
TOPIC_SCHEMA_MAP = {
    "gridmind.telemetry.solar": SolarTelemetrySchema,
    "gridmind.telemetry.wind": WindTelemetrySchema,
    "gridmind.telemetry.battery": BatteryTelemetrySchema,
    "gridmind.telemetry.meters": MetersTelemetrySchema,
    "gridmind.telemetry.market": MarketTelemetrySchema
}
