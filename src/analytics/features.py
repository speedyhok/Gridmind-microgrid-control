import math
import logging
import pandas as pd
import numpy as np
from datetime import datetime

from src.storage.db_client import GridMindDBClient
from src.simulator.weather import WeatherSimulator

logger = logging.getLogger(__name__)

class FeatureStore:
    def __init__(self, db_client: GridMindDBClient, weather_simulator: WeatherSimulator = None):
        self.db = db_client
        self.weather = weather_simulator or WeatherSimulator()

    def load_historical_data(self) -> pd.DataFrame:
        """
        Loads and joins prices, asset telemetry, and weather data from the database and cache.
        Returns a single master DataFrame indexed by time.
        """
        # 1. Load prices
        prices_raw = self.db.fetch_all("SELECT time, buy_price_inr, sell_price_inr, grid_frequency FROM electricity_prices ORDER BY time ASC;")
        if not prices_raw:
            # SQL is empty — try fallback loading from Parquet lake (fixes Flaw 7)
            df_lake = self.load_from_parquet_lake()
            if not df_lake.empty:
                return df_lake
            return pd.DataFrame()
            
        df_prices = pd.DataFrame(prices_raw, columns=["time", "buy_price_inr", "sell_price_inr", "grid_frequency"])
        df_prices["time"] = pd.to_datetime(df_prices["time"], format="ISO8601")
        df_prices.set_index("time", inplace=True)
        df_prices = df_prices[~df_prices.index.duplicated(keep='first')]

        # 2. Load telemetry for solar, wind, battery, and aggregate demand
        telemetry_raw = self.db.fetch_all("SELECT time, asset_id, power_kw, voltage, anomaly_flag FROM telemetry_power ORDER BY time ASC;")
        if not telemetry_raw:
            return df_prices # return prices only if no telemetry

        df_telemetry = pd.DataFrame(telemetry_raw, columns=["time", "asset_id", "power_kw", "voltage", "anomaly_flag"])
        df_telemetry["time"] = pd.to_datetime(df_telemetry["time"], format="ISO8601")

        # Pivot telemetry so we have asset columns (solar_power_kw, wind_power_kw, etc.)
        # solar
        df_solar = df_telemetry[df_telemetry["asset_id"] == "solar_pv"][["time", "power_kw"]].rename(columns={"power_kw": "solar_power_kw"}).set_index("time")
        df_solar = df_solar[~df_solar.index.duplicated(keep='first')]
        # wind
        df_wind = df_telemetry[df_telemetry["asset_id"] == "wind_turbine"][["time", "power_kw"]].rename(columns={"power_kw": "wind_power_kw"}).set_index("time")
        df_wind = df_wind[~df_wind.index.duplicated(keep='first')]
        # battery
        df_battery = df_telemetry[df_telemetry["asset_id"] == "battery_bank"][["time", "power_kw", "voltage"]].rename(columns={"power_kw": "battery_power_kw", "voltage": "battery_voltage"}).set_index("time")
        df_battery = df_battery[~df_battery.index.duplicated(keep='first')]
        # aggregate demand
        df_demand = df_telemetry[df_telemetry["asset_id"] == "campus_aggregate"][["time", "power_kw"]].rename(columns={"power_kw": "demand_power_kw"}).set_index("time")
        df_demand = df_demand[~df_demand.index.duplicated(keep='first')]

        # Join pivot tables with prices
        df_master = df_prices.join([df_solar, df_wind, df_battery, df_demand], how="left")
        
        # Fill missing values (for instances where some assets didn't log)
        df_master.ffill(inplace=True)
        df_master.bfill(inplace=True)
        df_master.fillna(0.0, inplace=True)

        # 3. Join weather variables from weather simulator cache for each timestamp
        weather_features = []
        for time_idx in df_master.index:
            conds = self.weather.get_current_conditions(time_idx.to_pydatetime())
            weather_features.append({
                "time": time_idx,
                "temperature_c": conds["temperature"],
                "wind_speed_ms": conds["wind_speed"],
                "irradiance_wm2": conds["irradiance"],
                "cloud_cover": conds["cloud_cover"]
            })
            
        df_weather = pd.DataFrame(weather_features).set_index("time")
        df_master = df_master.join(df_weather, how="left")

        return df_master

    def build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Engineers circular time encodings, lagged variables, moving averages, and physical wind speeds.
        """
        if df.empty:
            return df
            
        df = df.copy()

        # 1. Circular Time Encoding
        hours = df.index.hour
        df["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
        df["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)

        day_of_week = df.index.dayofweek
        df["day_sin"] = np.sin(2 * np.pi * day_of_week / 7.0)
        df["day_cos"] = np.cos(2 * np.pi * day_of_week / 7.0)
        df["is_weekend"] = (day_of_week >= 5).astype(int)

        # 2. Physics-Based Features
        df["wind_speed_cubed"] = df["wind_speed_ms"] ** 3
        # Solar attenuation index
        df["solar_attenuation"] = df["irradiance_wm2"] * (1.0 - 0.75 * (df["cloud_cover"] / 100.0) ** 3)

        # 3. Lag Features (1h, 2h, and 24h lags)
        targets = ["demand_power_kw", "solar_power_kw", "wind_power_kw", "buy_price_inr"]
        for target in targets:
            if target in df.columns:
                df[f"{target}_lag_1h"] = df[target].shift(1)
                df[f"{target}_lag_2h"] = df[target].shift(2)
                # Fill initial nan values from shift with backwards fill
                df[f"{target}_lag_1h"] = df[f"{target}_lag_1h"].bfill().fillna(0.0)
                df[f"{target}_lag_2h"] = df[f"{target}_lag_2h"].bfill().fillna(0.0)

        # 4. Rolling Window features (4h moving average)
        for target in targets:
            if target in df.columns:
                # shift first so that the rolling average only uses past values (no leakage)
                df[f"{target}_roll_4h"] = df[target].shift(1).rolling(window=4, min_periods=1).mean()
                df[f"{target}_roll_4h"] = df[f"{target}_roll_4h"].bfill().fillna(0.0)

        return df

    def load_from_parquet_lake(self) -> pd.DataFrame:
        """
        Reads and joins telemetry data directly from the Parquet files in data/lake/raw/.
        Serves as a robust analytical fallback or hybrid data source.
        """
        import os
        import glob
        try:
            lake_root = "data/lake"
            if not os.path.exists(lake_root):
                return pd.DataFrame()
                
            # Find all parquet files in the lake recursively
            files = glob.glob(os.path.join(lake_root, "**/*.parquet"), recursive=True)
            if not files:
                return pd.DataFrame()
                
            logger.info(f"Scanning Parquet Data Lake: found {len(files)} files...")
            
            # Read and group by topic
            dfs = []
            for file in files:
                try:
                    df_file = pd.read_parquet(file, engine="pyarrow")
                    # Deduce topic name from directory path
                    parts = os.path.normpath(file).split(os.sep)
                    # topic is the folder right under 'raw' or 'lake'
                    if "raw" in parts:
                        raw_idx = parts.index("raw")
                        if raw_idx + 1 < len(parts):
                            df_file["_topic"] = parts[raw_idx + 1]
                    elif "lake" in parts:
                        lake_idx = parts.index("lake")
                        if lake_idx + 1 < len(parts):
                            df_file["_topic"] = parts[lake_idx + 1]
                    dfs.append(df_file)
                except Exception as fe:
                    logger.warning(f"Failed to read parquet file {file}: {fe}")
                    
            if not dfs:
                return pd.DataFrame()
                
            df_all = pd.concat(dfs, ignore_index=True)
            if df_all.empty:
                return pd.DataFrame()
                
            df_all["timestamp"] = pd.to_datetime(df_all["timestamp"], format="ISO8601")
            df_all.set_index("timestamp", inplace=True)
            df_all.sort_index(inplace=True)
            
            # Construct pivoted master dataframe
            df_resampled = pd.DataFrame(index=df_all.index.unique()).sort_index()
            
            # Solar
            df_sol = df_all[df_all["_topic"] == "gridmind.telemetry.solar"]
            if not df_sol.empty:
                df_resampled["solar_power_kw"] = df_sol["solar_power_kw"]
                df_resampled["temperature_c"] = df_sol["temperature_c"]
                df_resampled["irradiance_wm2"] = df_sol["irradiance_wm2"]
                
            # Wind
            df_wnd = df_all[df_all["_topic"] == "gridmind.telemetry.wind"]
            if not df_wnd.empty:
                df_resampled["wind_power_kw"] = df_wnd["wind_power_kw"]
                df_resampled["wind_speed_ms"] = df_wnd["wind_speed_ms"]
                
            # Battery
            df_bat = df_all[df_all["_topic"] == "gridmind.telemetry.battery"]
            if not df_bat.empty:
                df_resampled["battery_power_kw"] = df_bat["battery_power_kw"]
                df_resampled["battery_voltage"] = df_bat["battery_voltage"]
                df_resampled["battery_soc_pct"] = df_bat["battery_soc"]
                
            # Campus Demand
            df_dem = df_all[df_all["_topic"] == "gridmind.telemetry.meters"]
            if not df_dem.empty:
                df_resampled["demand_power_kw"] = df_dem["demand_active_kw"]
                
            # Market Prices
            df_mkt = df_all[df_all["_topic"] == "gridmind.telemetry.market"]
            if not df_mkt.empty:
                df_resampled["buy_price_inr"] = df_mkt["electricity_buy_price_inr"]
                df_resampled["sell_price_inr"] = df_mkt["electricity_sell_price_inr"]
                df_resampled["grid_frequency"] = df_mkt["grid_frequency_hz"]
                
            df_resampled.ffill(inplace=True)
            df_resampled.bfill(inplace=True)
            df_resampled.fillna(0.0, inplace=True)
            logger.info(f"Loaded {len(df_resampled)} clean historical rows from Parquet Data Lake.")
            return df_resampled
        except Exception as e:
            logger.error(f"Error loading from Parquet Data Lake: {e}")
            return pd.DataFrame()

if __name__ == "__main__":
    db = GridMindDBClient()
    store = FeatureStore(db)
    raw_df = store.load_historical_data()
    if not raw_df.empty:
        feat_df = store.build_features(raw_df)
        print("Feature Columns:", list(feat_df.columns))
        print("Sample row:")
        print(feat_df.iloc[-1].to_dict())
    else:
        print("Historical database is empty.")
    db.close()
