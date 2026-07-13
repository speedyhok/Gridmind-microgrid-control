import os
import json
import argparse
import logging
from datetime import datetime, timedelta

from src.storage.db_client import GridMindDBClient
from src.simulator.config import BUILDINGS_CONFIG, SOLAR_AREA, SOLAR_EFFICIENCY, WIND_RATED_POWER, BATTERY_MAX_CHARGE_KW

logger = logging.getLogger(__name__)

class GridMindTelemetryWriter:
    def __init__(self, db_client: GridMindDBClient = None, seed_history: bool = True):
        self.db = db_client or GridMindDBClient()
        self.seed_assets()
        # Automatically seed history if database has fewer than 10 rows (fixes Flaw 4)
        if seed_history and not os.getenv("PYTEST_CURRENT_TEST"):
            self.seed_historical_telemetry(days=7)

    def seed_historical_telemetry(self, days: int = 7):
        """
        Seeds historical telemetry data for the past 'days' (default 7 days = 168 hours)
        directly into the active database. This ensures the ML models have enough data to train.
        """
        try:
            # Check if we already have sufficient telemetry
            row_count = self.db.fetch_all("SELECT count(*) FROM telemetry_power WHERE asset_id = 'campus_aggregate';")
            if row_count and row_count[0][0] >= days * 24:
                logger.info(f"Database already has {row_count[0][0]} historical data rows. Skipping seeding.")
                return

            logger.info(f"Database contains insufficient history ({row_count[0][0] if row_count else 0} rows). Seeding {days} days of data...")
            
            from src.simulator.battery import BatterySimulator
            from src.simulator.engine import run_single_tick
            
            battery_sim = BatterySimulator()
            end_time = datetime.now().replace(minute=0, second=0, microsecond=0)
            start_time = end_time - timedelta(days=days)
            
            # Loop hourly steps
            current_step = start_time
            records_count = 0
            while current_step < end_time:
                record = run_single_tick(step_time=current_step, battery_sim=battery_sim, dt_hours=1.0)
                
                # Write pricing
                self.write_pricing_record(
                    time_str=record["timestamp"],
                    buy_price=record["electricity_buy_price_inr"],
                    sell_price=record["electricity_sell_price_inr"],
                    freq=record["grid_frequency_hz"]
                )
                
                # Write main power records
                self.write_power_record(record["timestamp"], "solar_pv",        record["solar_power_kw"], None, "operational", record["anomaly_flag"])
                self.write_power_record(record["timestamp"], "wind_turbine",     record["wind_power_kw"],  None, "operational", record["anomaly_flag"])
                self.write_power_record(record["timestamp"], "battery_bank",     record["battery_power_kw"], record["battery_voltage"], "operational", record["anomaly_flag"], battery_soc_pct=record["battery_soc"])
                self.write_power_record(record["timestamp"], "campus_aggregate", record["demand_active_kw"], None, "operational", record["anomaly_flag"])
                
                # Write individual building breakdown
                for b_name, b_load in record.get("load_breakdown", {}).items():
                    self.write_power_record(record["timestamp"], b_name, b_load.get("active_power_kw", 0.0), None, "operational", False)
                
                current_step += timedelta(hours=1)
                records_count += 1
                
            logger.info(f"Successfully seeded {records_count} hours of historical telemetry.")
        except Exception as e:
            logger.error(f"Error seeding historical telemetry: {e}")

    def seed_assets(self):
        """Seeds the static assets metadata table on first boot."""
        try:
            # 1. Check if assets are already seeded
            assets = self.db.fetch_all("SELECT count(*) FROM assets;")
            if assets[0][0] > 0:
                logger.info("Assets table already seeded. Skipping initial seeding.")
                return

            logger.info("Seeding static assets metadata into database...")
            assets_to_seed = []
            
            # Solar
            assets_to_seed.append((
                "solar_pv",
                "solar",
                SOLAR_AREA * SOLAR_EFFICIENCY,
                "Microgrid PV Field"
            ))
            
            # Wind
            assets_to_seed.append((
                "wind_turbine",
                "wind",
                WIND_RATED_POWER,
                "North Ridge Turbine"
            ))
            
            # Battery
            assets_to_seed.append((
                "battery_bank",
                "battery",
                BATTERY_MAX_CHARGE_KW,
                "Central Battery Storage House"
            ))

            # Buildings from config
            for b_name, b_cfg in BUILDINGS_CONFIG.items():
                assets_to_seed.append((
                    b_name,
                    "building",
                    b_cfg["peak_kw"],
                    f"Campus {b_cfg['type'].title()} Building"
                ))

            # Unified aggregate asset
            assets_to_seed.append((
                "campus_aggregate",
                "building",
                sum(b["peak_kw"] for b in BUILDINGS_CONFIG.values()),
                "Total Campus Load Aggregate"
            ))

            # Bulk insert into assets
            insert_query = """
            INSERT INTO assets (asset_id, asset_type, capacity_kw, location)
            VALUES (%s, %s, %s, %s);
            """
            
            # sqlite syntax differs slightly for INSERT OR IGNORE, but standard INSERT works since we check count first
            self.db.executemany(insert_query, assets_to_seed)
            logger.info(f"Successfully seeded {len(assets_to_seed)} assets.")

        except Exception as e:
            logger.error(f"Error seeding assets: {e}")

    def write_pricing_record(self, time_str: str, buy_price: float, sell_price: float, freq: float):
        """Writes pricing and frequency data to the database, ignoring duplicates."""
        query = """
        INSERT INTO electricity_prices (time, buy_price_inr, sell_price_inr, grid_frequency)
        VALUES (%s, %s, %s, %s);
        """
        try:
            self.db.execute(query, (time_str, buy_price, sell_price, freq))
        except Exception as e:
            msg = str(e).lower()
            if "unique constraint" in msg or "duplicate key" in msg or "unique violation" in msg:
                logger.debug(f"Duplicate pricing record at {time_str} ignored.")
            else:
                raise e

    def write_power_record(self, time_str: str, asset_id: str, power_kw: float, voltage: float, status: str, anomaly: bool, battery_soc_pct: float = None):
        """Writes power telemetry metrics for an asset, ignoring duplicates."""
        query = """
        INSERT INTO telemetry_power (time, asset_id, power_kw, voltage, status, anomaly_flag, battery_soc_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
        """
        try:
            self.db.execute(query, (time_str, asset_id, power_kw, voltage, status, 1 if anomaly else 0, battery_soc_pct))
        except Exception as e:
            msg = str(e).lower()
            if "unique constraint" in msg or "duplicate key" in msg or "unique violation" in msg:
                logger.debug(f"Duplicate power record for {asset_id} at {time_str} ignored.")
            else:
                raise e

    def ingest_payload(self, topic: str, payload: dict):
        """
        Ingests a verified telemetry record. Maps topics to database writes.
        """
        time_str = payload.get("timestamp")
        anomaly = payload.get("anomaly_flag", False)

        if topic == "gridmind.telemetry.market":
            self.write_pricing_record(
                time_str=time_str,
                buy_price=payload["electricity_buy_price_inr"],
                sell_price=payload["electricity_sell_price_inr"],
                freq=payload["grid_frequency_hz"]
            )
            
        elif topic == "gridmind.telemetry.solar":
            self.write_power_record(
                time_str=time_str,
                asset_id="solar_pv",
                power_kw=payload["solar_power_kw"],
                voltage=None,
                status="operational" if payload["solar_power_kw"] > 0 or float(payload.get("irradiance_wm2", 0)) == 0.0 else "error",
                anomaly=anomaly
            )
            
        elif topic == "gridmind.telemetry.wind":
            self.write_power_record(
                time_str=time_str,
                asset_id="wind_turbine",
                power_kw=payload["wind_power_kw"],
                voltage=None,
                status="operational" if payload["wind_power_kw"] > 0 or float(payload.get("wind_speed_ms", 0)) < 3.0 else "error",
                anomaly=anomaly
            )
            
        elif topic == "gridmind.telemetry.battery":
            self.write_power_record(
                time_str=time_str,
                asset_id="battery_bank",
                power_kw=payload["battery_power_kw"],
                voltage=payload["battery_voltage"],
                status="operational" if float(payload.get("battery_soh", 100)) > 40.0 else "degraded",
                anomaly=anomaly,
                battery_soc_pct=float(payload.get("battery_soc", 0.0))
            )
            
        elif topic == "gridmind.telemetry.meters":
            # Write total active/reactive aggregate load
            self.write_power_record(
                time_str=time_str,
                asset_id="campus_aggregate",
                power_kw=payload["demand_active_kw"],
                voltage=None,
                status="operational",
                anomaly=anomaly
            )
            
            # Write individual building breakdown loads
            breakdown = payload.get("breakdown", {})
            for b_name, b_load in breakdown.items():
                self.write_power_record(
                    time_str=time_str,
                    asset_id=b_name,
                    power_kw=b_load["active_power_kw"],
                    voltage=None,
                    status="operational",
                    anomaly=anomaly
                )

    def close(self):
        self.db.close()

def run_db_writer_daemon():
    parser = argparse.ArgumentParser(description="GridMind Telemetry Database Writer Daemon")
    parser.add_argument("--source", type=str, default="file", choices=["file", "kafka"], help="Ingestion log source")
    parser.add_argument("--file-path", type=str, default="data/dead_letter_queue.log", help="Fallback DLQ file path to read")
    parser.add_argument("--sqlite-path", type=str, default="data/gridmind_storage.db", help="Local SQLite file path")
    args = parser.parse_args()

    # Initialize client and writer
    db_client = GridMindDBClient(sqlite_path=args.sqlite_path)
    writer = GridMindTelemetryWriter(db_client=db_client)

    if args.source == "file":
        logger.info(f"Starting Database Ingestion from fallback logs: {args.file_path}...")
        if not os.path.exists(args.file_path):
            logger.error(f"Fallback logs file {args.file_path} not found. Run the simulator streaming loop first.")
            return

        with open(args.file_path, "r") as f:
            lines = f.readlines()
            
        logger.info(f"Processing {len(lines)} log lines...")
        success_count = 0
        for idx, line in enumerate(lines):
            try:
                entry = json.loads(line)
                topic = entry.get("topic")
                payload = entry.get("payload")
                
                writer.ingest_payload(topic, payload)
                success_count += 1
            except Exception as e:
                logger.error(f"Error ingesting log line {idx}: {e}")

        logger.info(f"Ingestion finished. Loaded {success_count} logs into active database storage ({db_client.mode}).")
    
    writer.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_db_writer_daemon()
