import os
import json
import logging
from datetime import datetime, timedelta
import pandas as pd
import requests

from src.simulator.config import (
    LATITUDE, LONGITUDE, TIMEZONE, CACHE_FILE, CACHE_DIR,
    TEMP_MIN, TEMP_MAX, WIND_MIN, WIND_MAX
)

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

class WeatherSimulator:
    def __init__(self, lat: float = LATITUDE, lon: float = LONGITUDE):
        self.lat = lat
        self.lon = lon
        self.df = None
        self._load_weather_data()

    def _fetch_from_api(self) -> dict:
        """Fetches 7 days of forecast and historical-ish weather data from Open-Meteo."""
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,cloud_cover,direct_normal_irradiance,diffuse_radiation",
            "timezone": TIMEZONE
        }
        logger.info(f"Fetching real weather data from Open-Meteo for Lat: {self.lat}, Lon: {self.lon}...")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def _load_weather_data(self):
        """Loads weather data from local cache or fetches it if the cache is stale or missing."""
        try:
            # Check if cache file exists and is recent (less than 6 hours old)
            # Weather forecasts update on 6-hour cycles, so this keeps forecast data aligned and fresh.
            use_cache = False
            if os.path.exists(CACHE_FILE):
                file_mtime = datetime.fromtimestamp(os.path.getmtime(CACHE_FILE))
                if datetime.now() - file_mtime < timedelta(hours=6):
                    use_cache = True

            if use_cache:
                logger.info(f"Loading weather data from cache: {CACHE_FILE}")
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
            else:
                data = self._fetch_from_api()
                # Save to cache
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f, indent=2)
                logger.info(f"Successfully cached weather data to {CACHE_FILE}")

            # Process JSON into a Pandas DataFrame
            hourly_data = data.get("hourly", {})
            times = [datetime.fromisoformat(t) for t in hourly_data.get("time", [])]
            
            self.df = pd.DataFrame({
                "time": times,
                "temperature": hourly_data.get("temperature_2m", []),
                "humidity": hourly_data.get("relative_humidity_2m", []),
                "wind_speed": hourly_data.get("wind_speed_10m", []),
                "cloud_cover": hourly_data.get("cloud_cover", []),
                "dni": hourly_data.get("direct_normal_irradiance", []),
                "dhi": hourly_data.get("diffuse_radiation", [])
            })
            # Convert wind speed from km/h to m/s if Open-Meteo returned km/h (default is km/h, let's convert to m/s)
            # Open-Meteo default wind_speed_10m is km/h, let's scale it to m/s by dividing by 3.6
            self.df["wind_speed"] = self.df["wind_speed"] / 3.6
            
            # Compute total horizontal irradiance (GHI approximation = DNI * cos(zenith) + DHI)
            # For simplicity, we can use DNI + DHI as the total available irradiance
            self.df["irradiance"] = self.df["dni"] + self.df["dhi"]
            
            # Set time index for rapid lookup
            self.df.set_index("time", inplace=True)
            logger.info("Weather database successfully initialized from real telemetry.")

        except Exception as e:
            logger.error(f"Error loading weather API data: {e}. Falling back to physics equations.")
            self.df = None

    def get_current_conditions(self, timestamp: datetime) -> dict:
        """
        Returns weather conditions for the specified timestamp.
        Falls back to mathematical diurnal equations if the API data is unavailable.
        """
        # Clean timestamp to hourly resolution for matching
        hourly_ts = timestamp.replace(minute=0, second=0, microsecond=0)
        
        if self.df is not None:
            try:
                # Look for exact hour match or find the closest index
                if hourly_ts in self.df.index:
                    row = self.df.loc[hourly_ts]
                else:
                    # Find nearest index
                    idx = self.df.index.get_indexer([hourly_ts], method="nearest")[0]
                    row = self.df.iloc[idx]
                
                return {
                    "temperature": float(row["temperature"]),
                    "humidity": float(row["humidity"]),
                    "wind_speed": float(row["wind_speed"]),
                    "cloud_cover": float(row["cloud_cover"]),
                    "irradiance": float(row["irradiance"]),
                    "source": "real_api"
                }
            except Exception as e:
                logger.warning(f"Error looking up cached timestamp {hourly_ts}: {e}. Falling back to equations.")
        
        # Physics-based Fallback Generator
        hour = timestamp.hour
        # Diurnal temperature cycle: peaks at 15:00, coldest at 05:00
        temp_range = TEMP_MAX - TEMP_MIN
        temperature = TEMP_MIN + temp_range * (0.5 * (1.0 + (datetime.fromtimestamp(0) + timedelta(hours=hour - 15)).hour / 24.0)) # simple approximation
        # Better: sine wave peaking at 15:00 (hour 15)
        import math
        temp_fraction = (math.sin((hour - 9) / 24.0 * 2 * math.pi) + 1.0) / 2.0
        temperature = TEMP_MIN + temp_range * temp_fraction
        
        # Wind speed: basic random walk within bounds
        import random
        random.seed(timestamp.timestamp())
        wind_speed = WIND_MIN + (WIND_MAX - WIND_MIN) * random.random()
        
        # Irradiance model (Diurnal curve based on sunrise (6am) and sunset (6pm))
        sunrise = 6
        sunset = 18
        max_irradiance = 1000.0  # W/m2
        cloud_cover = 20.0       # Assume 20% cloud cover on fallback
        
        if sunrise < hour < sunset:
            # Diurnal sine curve
            irradiance_val = max_irradiance * math.sin(math.pi * (hour - sunrise) / (sunset - sunrise))
            # Cloud cover attenuation: (1 - 0.75 * Cc^3)
            irradiance = irradiance_val * (1.0 - 0.75 * (cloud_cover / 100.0) ** 3)
        else:
            irradiance = 0.0

        return {
            "temperature": round(temperature, 2),
            "humidity": 60.0,
            "wind_speed": round(wind_speed, 2),
            "cloud_cover": cloud_cover,
            "irradiance": round(irradiance, 2),
            "source": "physics_fallback"
        }

if __name__ == "__main__":
    sim = WeatherSimulator()
    now = datetime.now()
    print("Conditions now:")
    print(sim.get_current_conditions(now))
    print("Conditions at midnight:")
    print(sim.get_current_conditions(now.replace(hour=0)))
    print("Conditions at noon:")
    print(sim.get_current_conditions(now.replace(hour=12)))
