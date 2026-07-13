import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.storage.db_client import GridMindDBClient
from src.storage.db_writer import GridMindTelemetryWriter
from src.analytics.optimizer import GridMindOptimizer
from src.simulator.battery import BatterySimulator
from src.simulator.engine import run_single_tick

logger = logging.getLogger(__name__)

# Global state
db_client: Optional[GridMindDBClient] = None
battery_sim: Optional[BatterySimulator] = None
telemetry_writer: Optional[GridMindTelemetryWriter] = None

# Schedule cache — avoids re-solving MILP on every browser poll
_schedule_cache: Optional[list] = None
_schedule_cache_time: Optional[datetime] = None
_SCHEDULE_CACHE_TTL_SECONDS = 300  # Re-solve at most once every 5 minutes
_schedule_invalidated: bool = False   # Set True by live-update to force re-solve

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern FastAPI lifespan handler (replaces deprecated on_event startup/shutdown)."""
    global db_client, battery_sim, telemetry_writer
    # Startup
    db_client = GridMindDBClient()
    battery_sim = BatterySimulator()
    telemetry_writer = GridMindTelemetryWriter(db_client=db_client)
    logger.info("GridMind API startup complete.")
    yield
    # Shutdown
    if db_client is not None:
        db_client.close()
    logger.info("GridMind API shutdown complete.")

app = FastAPI(
    title="GridMind Microgrid Management API",
    description="REST backend service for live microgrid telemetry, time-series history, ML forecasts, and battery optimization schedules.",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files folder
app.mount("/static", StaticFiles(directory="src/api/static"), name="static")

# Enable CORS for frontend dashboard communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic schema for battery overrides
class BatteryOverrideRequest(BaseModel):
    command: str  # 'charge', 'discharge', 'none'
    rate_kw: float  # e.g., 0 to 500 kW

@app.get("/")
def redirect_to_dashboard():
    return RedirectResponse(url="/static/index.html")

@app.get("/health")

def health_check():
    return {
        "status": "healthy",
        "database_mode": db_client.mode if db_client else "offline"
    }

@app.get("/api/status")
def get_current_status():
    """
    Returns the latest status of the microgrid components.
    """
    if not db_client:
        raise HTTPException(status_code=500, detail="Database client offline.")

    try:
        now_str = datetime.now().isoformat()

        # 1. Fetch latest prices
        price_row = db_client.fetch_all(
            "SELECT time, buy_price_inr, sell_price_inr, grid_frequency FROM electricity_prices WHERE time <= %s ORDER BY time DESC LIMIT 1;",
            (now_str,)
        )
        
        # 2. Fetch latest telemetry per asset (each asset's most recent row independently)
        assets_telemetry = db_client.fetch_all(
            "SELECT t.asset_id, t.power_kw, t.voltage, t.status, t.anomaly_flag, t.time, t.battery_soc_pct "
            "FROM telemetry_power t "
            "INNER JOIN ("
            "  SELECT asset_id, max(time) AS max_time FROM telemetry_power WHERE time <= %s GROUP BY asset_id"
            ") latest ON t.asset_id = latest.asset_id AND t.time = latest.max_time;",
            (now_str,)
        )

        # Build response dict
        telemetry_map = {row[0]: {
            "power_kw": row[1],
            "voltage": row[2],
            "status": row[3],
            "anomaly_flag": bool(row[4]),
            "timestamp": row[5],
            "battery_soc_pct": row[6]  # non-null only for battery_bank rows
        } for row in assets_telemetry}

        latest_price = {}
        if price_row:
            latest_price = {
                "timestamp": price_row[0][0],
                "buy_price_inr": price_row[0][1],
                "sell_price_inr": price_row[0][2],
                "grid_frequency_hz": price_row[0][3]
            }

        return {
            "market_pricing": latest_price,
            "assets": telemetry_map
        }
    except Exception as e:
        logger.error(f"Error fetching current status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/metrics/historical")
def get_historical_metrics(
    asset_id: Optional[str] = Query(None, description="Filter metrics by asset ID (e.g. solar_pv, battery_bank, wind_turbine, campus_aggregate)"),
    limit: int = Query(50, ge=1, le=1000, description="Max records to return")
):
    """
    Returns historical data series for chart visualizations.
    """
    if not db_client:
        raise HTTPException(status_code=500, detail="Database client offline.")

    try:
        if asset_id:
            rows = db_client.fetch_all(
                "SELECT time, power_kw, voltage, status, anomaly_flag FROM telemetry_power "
                "WHERE asset_id = %s ORDER BY time DESC LIMIT %s;",
                (asset_id, limit)
            )
            data = [{
                "time": r[0],
                "power_kw": r[1],
                "voltage": r[2],
                "status": r[3],
                "anomaly": bool(r[4])
            } for r in rows]
        else:
            # Query price history
            rows = db_client.fetch_all(
                "SELECT time, buy_price_inr, sell_price_inr, grid_frequency FROM electricity_prices "
                "ORDER BY time DESC LIMIT %s;",
                (limit,)
            )
            data = [{
                "time": r[0],
                "buy_price_inr": r[1],
                "sell_price_inr": r[2],
                "grid_frequency_hz": r[3]
            } for r in rows]

        return {"asset_id": asset_id, "limit": limit, "data": data}
    except Exception as e:
        logger.error(f"Error fetching historical metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/schedule")
def get_optimal_schedule():
    """
    Returns the 24-hour battery optimization schedule.
    Result is cached for 5 minutes to avoid re-solving the MILP on every browser poll.
    Cache is invalidated immediately after a live-update tick changes battery state.
    """
    global _schedule_cache, _schedule_cache_time, _schedule_invalidated

    if not db_client:
        raise HTTPException(status_code=500, detail="Database client offline.")

    now = datetime.now()
    cache_age = (now - _schedule_cache_time).total_seconds() if _schedule_cache_time else float("inf")
    cache_valid = (_schedule_cache is not None
                   and cache_age < _SCHEDULE_CACHE_TTL_SECONDS
                   and not _schedule_invalidated)

    if cache_valid:
        logger.info(f"[Schedule] Returning cached schedule (age: {cache_age:.0f}s)")
        return _schedule_cache

    try:
        logger.info("[Schedule] Cache miss — running MILP optimizer...")
        optimizer = GridMindOptimizer(db_client)
        schedule = optimizer.optimize_schedule()
        if not schedule:
            raise HTTPException(status_code=404, detail="No forecast prediction logs found to run optimization.")
        _schedule_cache = schedule
        _schedule_cache_time = now
        _schedule_invalidated = False
        return schedule
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating optimal schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# API Key Security for Write Operations (fixes Flaw 10)
API_KEY = os.getenv("GRIDMIND_API_KEY", "gridmind_premium_secret_key")

def verify_api_key(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid or missing X-API-Key header. Access denied."
        )

@app.post("/api/control/override")
def apply_battery_override(request: BatteryOverrideRequest, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """
    Manually overrides the battery charge/discharge state. Saves state to local overrides file.
    Protected by X-API-Key token check.
    """
    verify_api_key(x_api_key)
    
    if request.command not in ["charge", "discharge", "none"]:
        raise HTTPException(status_code=400, detail="Invalid command. Choose 'charge', 'discharge', or 'none'.")
    if request.rate_kw < 0 or request.rate_kw > 500.0:
        raise HTTPException(status_code=400, detail="Override charge rate must be between 0 and 500 kW.")

    override_path = "data/control_overrides.json"
    os.makedirs(os.path.dirname(override_path), exist_ok=True)
    
    payload = {
        "command": request.command,
        "rate_kw": request.rate_kw,
        "updated_at": datetime.now().isoformat()
    }

    try:
        with open(override_path, "w") as f:
            json.dump(payload, f, indent=4)
        logger.info(f"Battery manual override set: {payload}")
        return {"status": "success", "override": payload}
    except Exception as e:
        logger.error(f"Error saving control override: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/live-update")
def trigger_live_update(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """
    Runs a single simulation tick using current weather and override state.
    Writes fresh telemetry to the database so the dashboard KPIs update immediately.
    Protected by X-API-Key token check.
    """
    verify_api_key(x_api_key)
    
    if not db_client or not battery_sim or not telemetry_writer:
        raise HTTPException(status_code=500, detail="Simulation engine not initialized.")

    try:
        step_time = datetime.now().replace(second=0, microsecond=0)
        record = run_single_tick(step_time=step_time, battery_sim=battery_sim)

        writer = telemetry_writer  # Use global instance — avoids seed_assets() overhead

        # Write pricing
        writer.write_pricing_record(
            time_str=record["timestamp"],
            buy_price=record["electricity_buy_price_inr"],
            sell_price=record["electricity_sell_price_inr"],
            freq=record["grid_frequency_hz"]
        )

        # Write solar, wind, battery, campus aggregate
        writer.write_power_record(record["timestamp"], "solar_pv",        record["solar_power_kw"], None, "operational", record["anomaly_flag"])
        writer.write_power_record(record["timestamp"], "wind_turbine",     record["wind_power_kw"],  None, "operational", record["anomaly_flag"])
        writer.write_power_record(record["timestamp"], "battery_bank",     record["battery_power_kw"], record["battery_voltage"], "operational", record["anomaly_flag"], battery_soc_pct=record["battery_soc"])
        writer.write_power_record(record["timestamp"], "campus_aggregate", record["demand_active_kw"], None, "operational", record["anomaly_flag"])

        # Write individual building loads
        for b_name, b_load in record.get("load_breakdown", {}).items():
            writer.write_power_record(record["timestamp"], b_name, b_load.get("active_power_kw", 0.0), None, "operational", False)

        # Invalidate schedule cache — next /api/schedule call will re-solve with new SoC (fixes Flaw 5)
        global _schedule_invalidated
        _schedule_invalidated = True

        logger.info(f"Live update tick at {record['timestamp']} | SoC={record['battery_soc']:.1f}% | Override={record['override_command']} | Schedule cache invalidated")

        return {
            "status": "ok",
            "timestamp": record["timestamp"],
            "solar_kw": record["solar_power_kw"],
            "wind_kw": record["wind_power_kw"],
            "demand_kw": record["demand_active_kw"],
            "battery_power_kw": record["battery_power_kw"],
            "battery_soc_pct": record["battery_soc"],
            "buy_price_inr": record["electricity_buy_price_inr"],
            "grid_frequency_hz": record["grid_frequency_hz"],
            "override_active": record["override_active"],
            "override_command": record["override_command"],
        }

    except Exception as e:
        logger.error(f"Live update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

