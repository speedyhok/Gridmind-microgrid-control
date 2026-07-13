import os
import json
import pickle
import logging
import argparse
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import math

from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score

from src.storage.db_client import GridMindDBClient
from src.simulator.weather import WeatherSimulator
from src.analytics.features import FeatureStore
from src.simulator.config import WIND_RATED_POWER

logger = logging.getLogger(__name__)

class GridMindForecaster:
    def __init__(self, db_client: GridMindDBClient, weather_simulator: WeatherSimulator = None):
        self.db = db_client
        self.weather = weather_simulator or WeatherSimulator()
        self.store = FeatureStore(self.db, self.weather)
        self.model_dir = "data/models"
        os.makedirs(self.model_dir, exist_ok=True)

        # Persisted or default models loading (fixes Flaw 18)
        self.models = {}
        targets = ["demand", "solar", "wind", "price"]
        defaults = {
            "demand": HistGradientBoostingRegressor(max_iter=100, random_state=42),
            "solar": RandomForestRegressor(n_estimators=50, random_state=42),
            "wind": LinearRegression(),
            "price": HistGradientBoostingRegressor(max_iter=100, random_state=42)
        }
        
        for target in targets:
            model_path = os.path.join(self.model_dir, f"{target}_model.pkl")
            if os.path.exists(model_path):
                try:
                    with open(model_path, "rb") as f:
                        self.models[target] = pickle.load(f)
                    logger.info(f"Loaded persisted model for '{target}' from {model_path}")
                except Exception as e:
                    logger.warning(f"Could not load persisted model for '{target}': {e}. Using default.")
                    self.models[target] = defaults[target]
            else:
                self.models[target] = defaults[target]
                
        self.metrics = {}

    def train_models(self) -> dict:
        """
        Loads data, builds features, trains the ML forecasters, and evaluates metrics.
        """
        # 1. Load feature dataframe
        raw_df = self.store.load_historical_data()
        if raw_df.empty or len(raw_df) < 10:
            logger.warning("Not enough historical data in database to train models. Minimum 10 rows required.")
            return {}

        df = self.store.build_features(raw_df)

        # Targets mapping to their feature columns
        target_features = {
            "demand": {
                "y": "demand_power_kw",
                "X": ["hour_sin", "hour_cos", "day_sin", "day_cos", "is_weekend", "demand_power_kw_lag_1h", "demand_power_kw_lag_2h", "demand_power_kw_roll_4h"]
            },
            "solar": {
                "y": "solar_power_kw",
                "X": ["hour_sin", "hour_cos", "temperature_c", "irradiance_wm2", "solar_attenuation"]
            },
            "wind": {
                "y": "wind_power_kw",
                "X": ["wind_speed_ms", "wind_speed_cubed"]
            },
            "price": {
                "y": "buy_price_inr",
                "X": ["hour_sin", "hour_cos", "day_sin", "day_cos", "is_weekend", "buy_price_inr_lag_1h", "buy_price_inr_lag_2h", "buy_price_inr_roll_4h"]
            }
        }

        # Train/Test Split (Simple chronological split: last 20% for testing)
        split_idx = int(len(df) * 0.8)
        train_df = df.iloc[:split_idx]
        test_df = df.iloc[split_idx:]
        
        # If dataset is too small to split, train and validate on the same small set
        if len(test_df) == 0:
            train_df = df
            test_df = df

        self.metrics = {}
        for target, config in target_features.items():
            y_col = config["y"]
            X_cols = config["X"]

            X_train, y_train = train_df[X_cols], train_df[y_col]
            X_test, y_test = test_df[X_cols], test_df[y_col]

            # Fit model
            logger.info(f"Training forecaster for target '{target}' using {len(X_train)} samples...")
            self.models[target].fit(X_train, y_train)

            # Persist model to disk (fixes Flaw 18)
            model_path = os.path.join(self.model_dir, f"{target}_model.pkl")
            try:
                with open(model_path, "wb") as f:
                    pickle.dump(self.models[target], f)
                logger.info(f"Persisted trained model for '{target}' to {model_path}")
            except Exception as e:
                logger.error(f"Failed to persist model for '{target}': {e}")

            # Evaluate
            y_pred = self.models[target].predict(X_test)
            
            # Constraints in evaluation (mimic production clamps)
            if target == "solar":
                # For test evaluations, check night irradiance and clamp
                y_pred = np.where(test_df["irradiance_wm2"] <= 0.0, 0.0, y_pred)
            elif target == "wind":
                y_pred = np.where((test_df["wind_speed_ms"] < 3.0) | (test_df["wind_speed_ms"] >= 25.0), 0.0, y_pred)
                y_pred = np.minimum(y_pred, WIND_RATED_POWER)
            y_pred = np.maximum(y_pred, 0.0)

            # Compute statistics
            mae = mean_absolute_error(y_test, y_pred)
            rmse = np.sqrt(mean_squared_error(y_test, y_pred))
            r2 = r2_score(y_test, y_pred) if len(y_test) > 1 else 1.0

            self.metrics[target] = {
                "dataset_size": len(df),
                "train_samples": len(X_train),
                "test_samples": len(X_test),
                "MAE": round(float(mae), 4),
                "RMSE": round(float(rmse), 4),
                "R2_Score": round(float(r2), 4)
            }
            logger.info(f"Model '{target}' stats -> MAE: {mae:.2f}, RMSE: {rmse:.2f}, R2: {r2:.2f}")

        # Save performance report to separate file
        self.save_performance_report()
        return self.metrics

    def save_performance_report(self, report_path: str = "data/ml_performance_report.md"):
        """Saves a detailed markdown report of the model metrics."""
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report = []
        report.append("# GridMind Machine Learning Model Performance Report")
        report.append(f"\nGenerated At: {now_str}")
        report.append("\nThis file catalogs the validation statistics for the 4 active microgrid forecasting models.")
        report.append("\n## Model Summary Table\n")
        report.append("| Target Forecaster | Algorithm | Training Samples | Test Samples | MAE | RMSE | R² Score |")
        report.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        
        algorithms = {
            "demand": "HistGradientBoostingRegressor",
            "solar": "RandomForestRegressor + Physics Override",
            "wind": "LinearRegression (with $v^3$)",
            "price": "HistGradientBoostingRegressor"
        }

        for target, stats in self.metrics.items():
            report.append(
                f"| **{target.title()}** | `{algorithms[target]}` | "
                f"{stats['train_samples']} | {stats['test_samples']} | "
                f"{stats['MAE']:.3f} | {stats['RMSE']:.3f} | {stats['R2_Score']:.3f} |"
            )

        report.append("\n## Metrics Glossary")
        report.append("* **MAE (Mean Absolute Error)**: The average absolute magnitude of prediction errors.")
        report.append("* **RMSE (Root Mean Squared Error)**: Square root of variance, heavily penalizing large outliers.")
        report.append("* **R² Score**: The coefficient of determination. 1.0 represents a perfect fit, while 0.0 represents predicting the mean.")

        with open(report_path, "w") as f:
            f.write("\n".join(report) + "\n")
        logger.info(f"Model performance report written to {report_path}")

    def generate_24h_forecast(self) -> list:
        """
        Executes recursive autoregressive forecasting for the next 24 hours.
        Applies hard physical clamps and writes results to the database.
        """
        # 1. Load latest database values for seeding lags
        raw_df = self.store.load_historical_data()
        if raw_df.empty:
            logger.error("No historical records available in DB to seed future lags.")
            return []

        df_feat = self.store.build_features(raw_df)
        latest_row = df_feat.iloc[-1]

        # Lags initialized from the latest known database step
        last_demand = float(latest_row["demand_power_kw"])
        last_demand_lag1 = float(latest_row["demand_power_kw_lag_1h"])
        last_price = float(latest_row["buy_price_inr"])
        last_price_lag1 = float(latest_row["buy_price_inr_lag_1h"])

        # Determine forecasting horizon (next 24 hours starting from latest timestamp)
        latest_time = df_feat.index[-1].to_pydatetime()
        created_at_str = datetime.now().isoformat()
        
        predictions_to_insert = []
        forecast_records = []

        logger.info(f"Generating future 24h predictions starting from: {latest_time + timedelta(hours=1)}")

        for h in range(1, 25):
            future_time = latest_time + timedelta(hours=h)
            timestamp_sec = future_time.timestamp()
            
            # Fetch future weather conditions from Open-Meteo cache
            weather = self.weather.get_current_conditions(future_time)
            temp = weather["temperature"]
            wind_speed = weather["wind_speed"]
            irradiance = weather["irradiance"]
            cloud_cover = weather["cloud_cover"]

            # Circular time calculations
            hour_val = future_time.hour
            weekday_val = future_time.weekday()
            is_wknd = 1 if weekday_val >= 5 else 0
            
            hour_sin = math.sin(2 * math.pi * hour_val / 24.0)
            hour_cos = math.cos(2 * math.pi * hour_val / 24.0)
            day_sin = math.sin(2 * math.pi * weekday_val / 7.0)
            day_cos = math.cos(2 * math.pi * weekday_val / 7.0)

            # Compute features matching training shapes
            solar_attn = irradiance * (1.0 - 0.75 * (cloud_cover / 100.0) ** 3)
            wind_cubed = wind_speed ** 3
            
            # 4h rolling average estimation based on recursive prediction values
            # (simple average of last lags)
            demand_roll = (last_demand + last_demand_lag1) / 2.0
            price_roll = (last_price + last_price_lag1) / 2.0

            # 2. Inference & Physics-Constrained Clamps
            
            # Target 1: Demand
            X_demand = [[hour_sin, hour_cos, day_sin, day_cos, is_wknd, last_demand, last_demand_lag1, demand_roll]]
            pred_demand = float(self.models["demand"].predict(X_demand)[0])
            pred_demand = max(5.0, pred_demand) # Demand clamp (must be positive, minimal campus baseline)

            # Target 2: Solar (Physics constrained)
            if irradiance <= 0.0 or hour_val < 6 or hour_val > 18:
                pred_solar = 0.0
            else:
                X_solar = [[hour_sin, hour_cos, temp, irradiance, solar_attn]]
                pred_solar = float(self.models["solar"].predict(X_solar)[0])
                pred_solar = max(0.0, pred_solar)

            # Target 3: Wind (Physics constrained)
            if wind_speed < 3.0 or wind_speed >= 25.0:
                pred_wind = 0.0
            else:
                X_wind = [[wind_speed, wind_cubed]]
                pred_wind = float(self.models["wind"].predict(X_wind)[0])
                pred_wind = max(0.0, min(pred_wind, WIND_RATED_POWER))

            # Target 4: Price
            X_price = [[hour_sin, hour_cos, day_sin, day_cos, is_wknd, last_price, last_price_lag1, price_roll]]
            pred_price = float(self.models["price"].predict(X_price)[0])
            pred_price = max(0.5, pred_price) # Price clamp (min 0.5 INR)

            # 3. Update Lags for recursive next hour step
            last_demand_lag1 = last_demand
            last_demand = pred_demand
            last_price_lag1 = last_price
            last_price = pred_price

            # Save prediction rows
            time_iso = future_time.isoformat()
            
            predictions_to_insert.append((time_iso, "demand", round(pred_demand, 3), created_at_str))
            predictions_to_insert.append((time_iso, "solar", round(pred_solar, 3), created_at_str))
            predictions_to_insert.append((time_iso, "wind", round(pred_wind, 3), created_at_str))
            predictions_to_insert.append((time_iso, "price", round(pred_price, 3), created_at_str))
            
            forecast_records.append({
                "time": time_iso,
                "demand": pred_demand,
                "solar": pred_solar,
                "wind": pred_wind,
                "price": pred_price
            })

        # 4. Bulk insert predictions to DB
        insert_query = """
        INSERT INTO predictions (time, target_type, predicted_value, created_at)
        VALUES (%s, %s, %s, %s);
        """
        self.db.executemany(insert_query, predictions_to_insert)
        logger.info(f"Successfully wrote {len(predictions_to_insert)} forecast values to predictions database.")
        
        return forecast_records

def run_ml_forecaster_cli():
    parser = argparse.ArgumentParser(description="GridMind Forecaster Execution CLI")
    parser.add_argument("--sqlite-path", type=str, default="data/gridmind_storage.db", help="Local SQLite file path")
    parser.add_argument("--report-path", type=str, default="data/ml_performance_report.md", help="Path to write stats report")
    args = parser.parse_args()

    db = GridMindDBClient(sqlite_path=args.sqlite_path)
    forecaster = GridMindForecaster(db_client=db)
    
    # 1. Train and evaluate
    metrics = forecaster.train_models()
    if metrics:
        # 2. Write 24-hour predictions
        forecaster.generate_24h_forecast()
    
    db.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_ml_forecaster_cli()
