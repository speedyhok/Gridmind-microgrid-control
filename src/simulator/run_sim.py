import os
import argparse
import logging
from datetime import datetime, timedelta
import pandas as pd
from colorama import init, Fore, Style

from src.simulator.config import LATITUDE, LONGITUDE
from src.simulator.weather import WeatherSimulator
from src.simulator.solar import SolarSimulator
from src.simulator.wind import WindTurbineSimulator
from src.simulator.battery import BatterySimulator
from src.simulator.meters import CampusMetersSimulator
from src.simulator.market import MarketSimulator
from src.ingestion.producer import KafkaTelemetryProducer

# Initialize colorama
init(autoreset=True)

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(description="GridMind Real-Time Microgrid IoT Simulator")
    parser.add_argument("--interval", type=int, default=1, help="Simulation step sleep interval in seconds")
    parser.add_argument("--duration", type=int, default=60, help="Total simulation steps to run")
    parser.add_argument("--step-size-mins", type=int, default=60, help="Simulated time increment per step in minutes")
    parser.add_argument("--lat", type=float, default=LATITUDE, help="Target latitude")
    parser.add_argument("--lon", type=float, default=LONGITUDE, help="Target longitude")
    parser.add_argument("--output-dir", type=str, default="data/sim_output", help="Directory to save telemetry files")
    parser.add_argument("--stream-kafka", action="store_true", help="Stream telemetry in real-time to Kafka topics")
    parser.add_argument("--kafka-broker", type=str, default="localhost:9092", help="Kafka broker bootstrap servers connection string")
    return parser.parse_args()

def run_simulation():
    args = parse_args()
    
    # 1. Initialize simulator modules
    weather_sim = WeatherSimulator(lat=args.lat, lon=args.lon)
    solar_sim = SolarSimulator()
    wind_sim = WindTurbineSimulator()
    battery_sim = BatterySimulator()
    meters_sim = CampusMetersSimulator()
    market_sim = MarketSimulator()
    
    # Initialize Kafka Producer if flag set
    producer = None
    if args.stream_kafka:
        producer = KafkaTelemetryProducer(bootstrap_servers=args.kafka_broker)

    # Starting simulation time (current time or fixed start)
    start_time = datetime.now().replace(minute=0, second=0, microsecond=0)
    current_time = start_time
    
    telemetry_history = []
    
    print(f"\n{Fore.CYAN}{Style.BRIGHT}======================================================================")
    print(f"{Fore.CYAN}{Style.BRIGHT}                GRIDMIND IoT SIMULATION RUNNER                       ")
    print(f"{Fore.CYAN}{Style.BRIGHT}======================================================================{Style.RESET_ALL}")
    print(f"Location coordinates: Lat {args.lat}, Lon {args.lon}")
    print(f"Simulation duration: {args.duration} steps, Step increment: {args.step_size_mins} mins")
    print(f"Logging telemetry to: {args.output_dir}/")
    print(f"Starting simulation time: {start_time}\n")

    # Header for live terminal logs
    print(f"{Fore.YELLOW}{Style.BRIGHT}{'Timestamp':<20} | {'Temp':<5} | {'Wind':<5} | {'Solar':<8} | {'WindPow':<8} | {'Demand':<8} | {'Battery':<7} | {'Buy':<5} | {'Freq':<6}")
    print("-" * 105)

    for step in range(args.duration):
        # Calculate current time for this step
        step_time = current_time + timedelta(minutes=step * args.step_size_mins)
        timestamp_sec = step_time.timestamp()
        
        # 2. Get Weather
        weather = weather_sim.get_current_conditions(step_time)
        temp = weather["temperature"]
        wind_speed = weather["wind_speed"]
        irradiance = weather["irradiance"]
        
        # 3. Calculate Solar & Wind Yield
        solar_kw = solar_sim.calculate_power(irradiance, temp)
        wind_kw, wind_wear = wind_sim.calculate_power_and_wear(wind_speed, timestamp_sec)
        
        # 4. Get Campus Load
        load = meters_sim.get_total_campus_load(step_time)
        demand_kw = load["total_active_power_kw"]
        reactive_kvar = load["total_reactive_power_kvar"]
        agg_pf = load["aggregate_power_factor"]
        
        # 5. Get Grid Market Pricing & State
        market = market_sim.get_market_state(step_time)
        buy_price = market["buy_price_inr"]
        sell_price = market["sell_price_inr"]
        freq = market["grid_frequency_hz"]
        
        # 6. Dispatch Strategy (Battery Power Flow)
        # Charge battery with excess generation, discharge battery if short
        net_generation = solar_kw + wind_kw
        deficit = demand_kw - net_generation
        
        charge_demand = 0.0
        discharge_demand = 0.0
        if deficit < 0:
            charge_demand = abs(deficit)  # Surplus goes to battery
        else:
            discharge_demand = deficit     # Deficit drawn from battery
            
        battery_state = battery_sim.step(
            charge_demand_kw=charge_demand,
            discharge_demand_kw=discharge_demand,
            ambient_temp=temp,
            dt_hours=args.step_size_mins / 60.0
        )
        
        battery_power = battery_state["power_kw"]  # Positive means charging, negative discharging
        soc = battery_state["soc"]
        soh = battery_state["soh"]
        cell_temp = battery_state["cell_temp"]
        anomaly = battery_state["anomaly_flag"]
        
        # 7. Final Grid Import/Export after battery compensation
        # If charging, power flow is grid import
        # net grid power = deficit + battery_power
        grid_import_kw = deficit + battery_power
        
        # 8. Store Telemetry Record
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
            "weather_source": weather["source"]
        }
        telemetry_history.append(record)
        
        # Publish to Kafka if streaming enabled, otherwise write locally to Parquet data lake fallback (fixes Flaw 7)
        if args.stream_kafka and producer is not None:
            producer.publish("gridmind.telemetry.solar", {
                "timestamp": record["timestamp"],
                "temperature_c": record["temperature_c"],
                "irradiance_wm2": record["irradiance_wm2"],
                "solar_power_kw": record["solar_power_kw"],
                "weather_source": record["weather_source"]
            })
            producer.publish("gridmind.telemetry.wind", {
                "timestamp": record["timestamp"],
                "wind_speed_ms": record["wind_speed_ms"],
                "wind_power_kw": record["wind_power_kw"],
                "wind_vibration": record["wind_vibration"]
            })
            producer.publish("gridmind.telemetry.battery", {
                "timestamp": record["timestamp"],
                "battery_power_kw": record["battery_power_kw"],
                "battery_soc": record["battery_soc"],
                "battery_soh": record["battery_soh"],
                "battery_cell_temp": record["battery_cell_temp"],
                "battery_voltage": record["battery_voltage"],
                "anomaly_flag": record["anomaly_flag"]
            })
            producer.publish("gridmind.telemetry.meters", {
                "timestamp": record["timestamp"],
                "demand_active_kw": record["demand_active_kw"],
                "demand_reactive_kvar": record["demand_reactive_kvar"],
                "aggregate_power_factor": record["aggregate_power_factor"],
                "breakdown": load["breakdown"]
            })
            producer.publish("gridmind.telemetry.market", {
                "timestamp": record["timestamp"],
                "electricity_buy_price_inr": record["electricity_buy_price_inr"],
                "electricity_sell_price_inr": record["electricity_sell_price_inr"],
                "grid_frequency_hz": record["grid_frequency_hz"]
            })
        else:
            try:
                from src.streaming.writer import DataLakeWriter
                lake_writer = DataLakeWriter()
                lake_writer.write_record("gridmind.telemetry.solar", {
                    "timestamp": record["timestamp"],
                    "temperature_c": record["temperature_c"],
                    "irradiance_wm2": record["irradiance_wm2"],
                    "solar_power_kw": record["solar_power_kw"],
                    "weather_source": record["weather_source"]
                })
                lake_writer.write_record("gridmind.telemetry.wind", {
                    "timestamp": record["timestamp"],
                    "wind_speed_ms": record["wind_speed_ms"],
                    "wind_power_kw": record["wind_power_kw"],
                    "wind_vibration": record["wind_vibration"]
                })
                lake_writer.write_record("gridmind.telemetry.battery", {
                    "timestamp": record["timestamp"],
                    "battery_power_kw": record["battery_power_kw"],
                    "battery_soc": record["battery_soc"],
                    "battery_soh": record["battery_soh"],
                    "battery_cell_temp": record["battery_cell_temp"],
                    "battery_voltage": record["battery_voltage"],
                    "anomaly_flag": record["anomaly_flag"]
                })
                lake_writer.write_record("gridmind.telemetry.meters", {
                    "timestamp": record["timestamp"],
                    "demand_active_kw": record["demand_active_kw"],
                    "demand_reactive_kvar": record["demand_reactive_kvar"],
                    "aggregate_power_factor": record["aggregate_power_factor"],
                    "breakdown": load["breakdown"]
                })
                lake_writer.write_record("gridmind.telemetry.market", {
                    "timestamp": record["timestamp"],
                    "electricity_buy_price_inr": record["electricity_buy_price_inr"],
                    "electricity_sell_price_inr": record["electricity_sell_price_inr"],
                    "grid_frequency_hz": record["grid_frequency_hz"]
                })
            except Exception as e:
                logger.warning(f"Failed programmatically writing to Parquet data lake fallback: {e}")

        # 9. Format Console Output with Colors
        color_solar = Fore.GREEN if solar_kw > 0 else Fore.LIGHTBLACK_EX
        color_wind = Fore.GREEN if wind_kw > 0 else Fore.LIGHTBLACK_EX
        color_battery = Fore.GREEN if battery_power < 0 else (Fore.BLUE if battery_power > 0 else Fore.LIGHTBLACK_EX)
        color_anomaly = f"{Fore.RED}{Style.BRIGHT}" if anomaly else ""
        
        print(
            f"{step_time.strftime('%Y-%m-%d %H:%M'):<20} | "
            f"{temp:<5.1f} | "
            f"{wind_speed:<5.1f} | "
            f"{color_solar}{solar_kw:<8.1f}{Style.RESET_ALL} | "
            f"{color_wind}{wind_kw:<8.1f}{Style.RESET_ALL} | "
            f"{Fore.MAGENTA}{demand_kw:<8.1f}{Style.RESET_ALL} | "
            f"{color_battery}{soc:>3.0f}% ({battery_power:+.0f}){Style.RESET_ALL}{color_anomaly} | "
            f"{Fore.YELLOW}{buy_price:<5.2f}{Style.RESET_ALL} | "
            f"{freq:<6.3f}"
        )
        
        # Sim step delay (optional)
        # if args.interval > 0:
        #     time.sleep(args.interval)

    # 10. Serialize outputs
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Save JSON
    json_path = os.path.join(args.output_dir, "telemetry_history.json")
    df = pd.DataFrame(telemetry_history)
    df.to_json(json_path, orient="records", indent=2)
    
    # Save CSV
    csv_path = os.path.join(args.output_dir, "telemetry_history.csv")
    df.to_csv(csv_path, index=False)
    
    if args.stream_kafka and producer is not None:
        producer.close()

    print(f"\n{Fore.GREEN}{Style.BRIGHT}Simulation completed successfully!")
    print(f"Saved {len(telemetry_history)} records to:")
    print(f"  JSON: {json_path}")
    print(f"  CSV:  {csv_path}\n")

if __name__ == "__main__":
    run_simulation()
