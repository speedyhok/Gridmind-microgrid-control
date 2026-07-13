import os
import logging
from datetime import datetime
import pandas as pd

logger = logging.getLogger(__name__)

class DataLakeWriter:
    def __init__(self, root_path: str = "data/lake"):
        self.root_path = root_path
        os.makedirs(self.root_path, exist_ok=True)

    def write_record(self, topic: str, record: dict) -> str:
        """
        Saves a single telemetry record as a partitioned Parquet file.
        Folder structure:
          {root_path}/raw/{topic}/year={YYYY}/month={MM}/day={DD}/hour={HH}/
        Filename:
          telemetry_{timestamp_epoch}.parquet
        """
        try:
            # Parse timestamp to determine partitions
            ts_str = record.get("timestamp")
            if isinstance(ts_str, str):
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            elif isinstance(ts, datetime):
                ts = ts_str
            else:
                ts = datetime.now()

            # Create partition structure
            year = ts.strftime("%Y")
            month = ts.strftime("%m")
            day = ts.strftime("%d")
            hour = ts.strftime("%H")
            
            partition_dir = os.path.join(
                self.root_path,
                "raw",
                topic,
                f"year={year}",
                f"month={month}",
                f"day={day}",
                f"hour={hour}"
            )
            os.makedirs(partition_dir, exist_ok=True)

            # Create dataframe from single record
            df = pd.DataFrame([record])
            
            # Ensure timestamps are serialized as datetime objects for Parquet
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"])

            # Generate unique filename based on record time to prevent overwrites
            epoch_time = int(ts.timestamp())
            file_path = os.path.join(partition_dir, f"telemetry_{epoch_time}.parquet")
            
            # Write to Parquet using pyarrow engine
            df.to_parquet(file_path, engine="pyarrow", index=False)
            return file_path

        except Exception as e:
            logger.error(f"Failed to write record to Data Lake: {e}")
            raise e

if __name__ == "__main__":
    writer = DataLakeWriter()
    test_record = {
        "timestamp": datetime.now().isoformat(),
        "temperature_c": 26.5,
        "irradiance_wm2": 500.0,
        "solar_power_kw": 40.0,
        "weather_source": "test"
    }
    path = writer.write_record("gridmind.telemetry.solar", test_record)
    print(f"Test Parquet written to: {path}")
