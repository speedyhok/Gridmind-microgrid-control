import os
import json
import argparse
import logging
from collections import deque
from datetime import datetime
from pydantic import ValidationError

from src.streaming.schemas import TOPIC_SCHEMA_MAP
from src.streaming.writer import DataLakeWriter

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

class TelemetryValidatorEngine:
    def __init__(self, lake_root: str = "data/lake", window_size: int = 5):
        self.writer = DataLakeWriter(root_path=lake_root)
        self.window_size = window_size
        
        # History tracking for frozen sensor detection: self.history[topic][field_name] -> deque
        self.history = {}
        # History tracking for running averages (imputation): self.valid_history[topic][field_name] -> deque
        self.valid_history = {}

    def _update_history(self, topic: str, payload: dict):
        """Helper to append recent telemetry values into sliding windows."""
        if topic not in self.history:
            self.history[topic] = {}
            self.valid_history[topic] = {}

        for k, v in payload.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                # 1. Update general sliding window for flatline checks
                if k not in self.history[topic]:
                    self.history[topic][k] = deque(maxlen=self.window_size)
                self.history[topic][k].append(v)
                
                # 2. Update valid state window for running averages imputation
                if k not in self.valid_history[topic]:
                    self.valid_history[topic][k] = deque(maxlen=3)
                self.valid_history[topic][k].append(v)

    def detect_anomalies_and_validate(self, topic: str, raw_payload: dict) -> dict:
        """
        Runs schema validation, handles imputation for missing values, 
        and runs anomaly detection logic (frozen sensors & outliers).
        """
        # --- 1. Imputation (Handle missing values before validation) ---
        clean_payload = raw_payload.copy()
        schema_cls = TOPIC_SCHEMA_MAP.get(topic)
        if not schema_cls:
            raise ValueError(f"No schema configured for topic: {topic}")
            
        schema_fields = schema_cls.model_fields
        for field in schema_fields:
            if field not in clean_payload or clean_payload[field] is None:
                # Impute numerical values using valid history running averages
                if topic in self.valid_history and field in self.valid_history[topic] and len(self.valid_history[topic][field]) > 0:
                    history_vals = self.valid_history[topic][field]
                    imputed_val = round(sum(history_vals) / len(history_vals), 3)
                    clean_payload[field] = imputed_val
                    logger.warning(f"Imputed missing field '{field}' in topic '{topic}' with running average: {imputed_val}")
                elif field == "anomaly_flag":
                    clean_payload[field] = False
                elif field == "weather_source":
                    clean_payload[field] = "imputed"

        # --- 2. Schema Validation ---
        try:
            validated_model = schema_cls(**clean_payload)
            # Dump to dict using serializable formats
            validated_payload = json.loads(validated_model.model_dump_json())
        except ValidationError as ve:
            logger.error(f"Schema validation FAILED for topic {topic} with payload {clean_payload}: {ve}")
            raise ve

        # --- 3. Outlier / Safety Threshold Checks ---
        anomaly_detected = False
        
        # Solar Panel Temp check
        if topic == "gridmind.telemetry.solar":
            if validated_payload.get("temperature_c", 0.0) > 75.0:
                logger.warning(f"OUTLIER ALERT: High Solar panel temperature: {validated_payload['temperature_c']}°C")
                anomaly_detected = True
        
        # Wind Turbine Cutoff check
        elif topic == "gridmind.telemetry.wind":
            if validated_payload.get("wind_speed_ms", 0.0) > 45.0:
                logger.warning(f"OUTLIER ALERT: Extreme storm wind speed: {validated_payload['wind_speed_ms']} m/s")
                anomaly_detected = True
        
        # Battery run-away thermal check
        elif topic == "gridmind.telemetry.battery":
            if validated_payload.get("battery_cell_temp", 0.0) > 60.0:
                logger.warning(f"OUTLIER ALERT: High battery cell temp (runaway risk): {validated_payload['battery_cell_temp']}°C")
                anomaly_detected = True
            if validated_payload.get("anomaly_flag", False):
                anomaly_detected = True
                
        # Grid frequency limits check
        elif topic == "gridmind.telemetry.market":
            freq = validated_payload.get("grid_frequency_hz", 50.0)
            if freq < 49.5 or freq > 50.5:
                logger.warning(f"OUTLIER ALERT: Grid frequency unstable: {freq} Hz")
                anomaly_detected = True

        # --- 4. Frozen Sensor (Flatline) Detection ---
        # Update history queues with validated values
        self._update_history(topic, validated_payload)

        # Check for flatlines
        if topic in self.history:
            for field, vals in self.history[topic].items():
                if len(vals) >= self.window_size:
                    # Exclude solar zero values at night from flatline warnings
                    if field in ["irradiance_wm2", "solar_power_kw", "wind_power_kw", "battery_power_kw"] and max(vals) == 0.0:
                        continue
                    
                    # If all values in the sliding window are identical
                    if len(set(vals)) == 1:
                        logger.warning(f"ANOMALY ALERT: Flatline/Frozen sensor detected on '{topic}' -> field '{field}' value: {vals[0]}")
                        anomaly_detected = True

        # Set consolidated anomaly flag
        validated_payload["anomaly_flag"] = anomaly_detected
        return validated_payload

    def process_and_archive(self, topic: str, raw_payload: dict) -> str:
        """Runs the validation pipeline and saves clean data to Parquet."""
        clean_record = self.detect_anomalies_and_validate(topic, raw_payload)
        parquet_path = self.writer.write_record(topic, clean_record)
        return parquet_path

def run_validator_daemon():
    parser = argparse.ArgumentParser(description="GridMind Telemetry Validation Daemon")
    parser.add_argument("--source", type=str, default="file", choices=["file", "kafka"], help="Telemetry input source")
    parser.add_argument("--file-path", type=str, default="data/dead_letter_queue.log", help="Fallback DLQ file path to read")
    parser.add_argument("--lake-root", type=str, default="data/lake", help="Data lake root storage path")
    args = parser.parse_args()

    engine = TelemetryValidatorEngine(lake_root=args.lake_root)

    if args.source == "file":
        logger.info(f"Starting Validator in file ingestion mode reading from {args.file_path}...")
        if not os.path.exists(args.file_path):
            logger.error(f"Target DLQ backup file {args.file_path} does not exist. Run the simulator stream first.")
            return

        with open(args.file_path, "r") as f:
            lines = f.readlines()
            
        logger.info(f"Loaded {len(lines)} raw records from fallback backups. Processing...")
        written_count = 0
        
        for idx, line in enumerate(lines):
            try:
                entry = json.loads(line)
                topic = entry.get("topic")
                payload = entry.get("payload")
                
                # Run pipeline
                path = engine.process_and_archive(topic, payload)
                written_count += 1
            except Exception as e:
                logger.error(f"Error processing DLQ log line {idx}: {e}")

        logger.info(f"Ingestion completed. Successfully archived {written_count} partitions to Parquet lake.")

if __name__ == "__main__":
    run_validator_daemon()
