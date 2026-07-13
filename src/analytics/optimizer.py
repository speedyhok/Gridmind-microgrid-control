import logging
import argparse
from datetime import datetime, timedelta
import pandas as pd
from pulp import LpProblem, LpMinimize, LpVariable, lpSum, LpStatus, PULP_CBC_CMD

from src.storage.db_client import GridMindDBClient
from src.simulator.config import BATTERY_MAX_CHARGE_KW, BATTERY_CAPACITY_KWH, GRID_PRICE_BUY_BASE, GRID_PRICE_SELL_BASE

logger = logging.getLogger(__name__)

class GridMindOptimizer:
    def __init__(self, db_client: GridMindDBClient):
        self.db = db_client

    def fetch_latest_forecasts(self) -> pd.DataFrame:
        """
        Retrieves the latest 24-hour predictions for solar, wind, demand, and price from the database.
        Runs auto-forecasting if predictions are missing or older than 1 hour.
        Returns a pivoted DataFrame.
        """
        # Fetch the latest batch run timestamp
        latest_run = self.db.fetch_all("SELECT max(created_at) FROM predictions;")
        
        # Check if we need to auto-generate predictions (missing or > 1 hour old)
        needs_forecast = False
        if not latest_run or latest_run[0][0] is None:
            needs_forecast = True
            logger.info("[Optimizer] No forecasts found in database. Auto-triggering ML forecaster...")
        else:
            try:
                created_at_dt = datetime.fromisoformat(latest_run[0][0])
                if (datetime.now() - created_at_dt).total_seconds() > 3600:
                    needs_forecast = True
                    logger.info("[Optimizer] Forecasts are older than 1 hour. Auto-triggering ML retrain...")
            except Exception:
                needs_forecast = True

        if needs_forecast:
            try:
                from src.analytics.forecaster import GridMindForecaster
                forecaster = GridMindForecaster(db_client=self.db)
                metrics = forecaster.train_models()
                if metrics:
                    forecaster.generate_24h_forecast()
                    # Re-fetch latest run
                    latest_run = self.db.fetch_all("SELECT max(created_at) FROM predictions;")
            except Exception as e:
                logger.error(f"[Optimizer] Failed to auto-generate ML predictions: {e}")

        if not latest_run or latest_run[0][0] is None:
            logger.warning("[Optimizer] Still no predictions found after auto-trigger attempt.")
            return pd.DataFrame()

        created_at_time = latest_run[0][0]
        
        # Load all predictions for this run
        rows = self.db.fetch_all(
            "SELECT time, target_type, predicted_value FROM predictions WHERE created_at = %s ORDER BY time ASC;",
            (created_at_time,)
        )

        df = pd.DataFrame(rows, columns=["time", "target_type", "value"])
        df["time"] = pd.to_datetime(df["time"])
        
        # Pivot targets (demand, solar, wind, price) into columns
        df_pivot = df.pivot(index="time", columns="target_type", values="value")
        return df_pivot

    def get_latest_battery_soc(self) -> float:
        """
        Reads the actual latest battery SoC from the telemetry_power table (battery_soc_pct column).
        Converts SoC% to kWh using the 2000 kWh nominal capacity.
        Falls back to 1000 kWh (50%) if no SoC telemetry exists yet.
        """
        rows = self.db.fetch_all(
            "SELECT battery_soc_pct FROM telemetry_power "
            "WHERE asset_id = 'battery_bank' AND battery_soc_pct IS NOT NULL AND time <= %s "
            "ORDER BY time DESC LIMIT 1;",
            (datetime.now().isoformat(),)
        )
        if rows and rows[0][0] is not None:
            soc_pct = float(rows[0][0])
            soc_kwh = (soc_pct / 100.0) * 2000.0  # BATTERY_CAPACITY_KWH = 2000
            logger.info(f"[Optimizer] Using live battery SoC: {soc_pct:.1f}% = {soc_kwh:.1f} kWh")
            return soc_kwh
        logger.warning("[Optimizer] No live SoC data found; defaulting to 50% (1000 kWh).")
        return 1000.0

    def optimize_schedule(self, initial_soc: float = None) -> list:
        """
        Formulates and solves the MILP battery scheduling problem for the 24-hour horizon.
        """
        df = self.fetch_latest_forecasts()
        if df.empty:
            return []

        if initial_soc is None:
            initial_soc = self.get_latest_battery_soc()

        # Horizon length (usually 24 hours)
        N = len(df)
        times = df.index.tolist()

        # Problem Definition
        prob = LpProblem("GridMind_Battery_Optimization", LpMinimize)

        # Decision Variables
        p_buy = [LpVariable(f"p_buy_{t}", lowBound=0.0) for t in range(N)]
        p_sell = [LpVariable(f"p_sell_{t}", lowBound=0.0) for t in range(N)]
        p_charge = [LpVariable(f"p_charge_{t}", lowBound=0.0, upBound=BATTERY_MAX_CHARGE_KW) for t in range(N)]
        p_discharge = [LpVariable(f"p_discharge_{t}", lowBound=0.0, upBound=BATTERY_MAX_CHARGE_KW) for t in range(N)]
        
        # SoC Bounds: 10% min to 90% max capacity
        soc_min = 0.1 * BATTERY_CAPACITY_KWH # 200 kWh
        soc_max = 0.9 * BATTERY_CAPACITY_KWH # 1800 kWh
        soc = [LpVariable(f"soc_{t}", lowBound=soc_min, upBound=soc_max) for t in range(N)]
        
        # Binary variables to prevent simultaneous charging and discharging
        u = [LpVariable(f"u_{t}", cat="Binary") for t in range(N)]

        # Objective Function: Minimize net cost of electricity
        # Net Cost = sum(buy_price * p_buy - sell_price * p_sell)
        sell_ratio = GRID_PRICE_SELL_BASE / GRID_PRICE_BUY_BASE
        costs = []
        for t in range(N):
            buy_p = df.iloc[t]["price"]
            sell_p = sell_ratio * buy_p
            costs.append(buy_p * p_buy[t] - sell_p * p_sell[t])
        prob += lpSum(costs)

        # Efficiency terms
        eta_in = 0.95
        eta_out = 0.95

        # Constraints
        for t in range(N):
            demand = df.iloc[t]["demand"]
            solar = df.iloc[t]["solar"]
            wind = df.iloc[t]["wind"]

            # 1. Power Balance
            prob += (p_buy[t] + solar + wind + p_discharge[t] == demand + p_charge[t] + p_sell[t])

            # 2. Battery Interlock constraints
            prob += (p_charge[t] <= BATTERY_MAX_CHARGE_KW * u[t])
            prob += (p_discharge[t] <= BATTERY_MAX_CHARGE_KW * (1 - u[t]))

            # 3. SoC dynamics
            if t == 0:
                prob += (soc[0] == initial_soc + (eta_in * p_charge[0] - p_discharge[0] / eta_out))
            else:
                prob += (soc[t] == soc[t-1] + (eta_in * p_charge[t] - p_discharge[t] / eta_out))

        # Solve Model
        status = prob.solve(PULP_CBC_CMD(msg=False))
        
        if LpStatus[status] != "Optimal":
            logger.error(f"Solver failed to find an optimal solution. Status: {LpStatus[status]}")
            return []

        # Parse schedule results
        schedule = []
        for t in range(N):
            buy_val = p_buy[t].varValue
            sell_val = p_sell[t].varValue
            charge_val = p_charge[t].varValue
            discharge_val = p_discharge[t].varValue
            soc_val = soc[t].varValue
            
            buy_price = df.iloc[t]["price"]
            demand = df.iloc[t]["demand"]
            solar = df.iloc[t]["solar"]
            wind = df.iloc[t]["wind"]
            
            # Net generation
            net_renewables = solar + wind
            
            # Baseline cost (buying directly from grid without battery optimization)
            net_baseline_power = max(0.0, demand - net_renewables)
            baseline_hourly_cost = net_baseline_power * buy_price
            
            # Optimized cost
            opt_hourly_cost = buy_val * buy_price - sell_val * (0.7 * buy_price)

            schedule.append({
                "hour": times[t].strftime("%Y-%m-%d %H:%M"),
                "buy_price_inr": round(buy_price, 2),
                "demand_kw": round(demand, 1),
                "solar_kw": round(solar, 1),
                "wind_kw": round(wind, 1),
                "charge_kw": round(charge_val, 1),
                "discharge_kw": round(discharge_val, 1),
                "battery_soc_kwh": round(soc_val, 1),
                "grid_buy_kw": round(buy_val, 1),
                "grid_sell_kw": round(sell_val, 1),
                "baseline_cost_inr": round(baseline_hourly_cost, 2),
                "optimized_cost_inr": round(opt_hourly_cost, 2)
            })

        return schedule

def display_optimal_schedule(schedule: list):
    """Prints a beautiful Bloomberg-style ASCII table of the optimized battery commands."""
    if not schedule:
        print("No schedule to display.")
        return

    print("=" * 125)
    print(f"| {'HOUR':<17} | {'PRICE':<6} | {'DEMAND':<7} | {'SOLAR':<6} | {'WIND':<6} | {'CHARGE':<6} | {'DISCHG':<6} | {'SOC (kWh)':<9} | {'BUY':<6} | {'SELL':<6} | {'BASE COST':<9} | {'OPT COST':<8} |")
    print("=" * 125)
    
    total_base_cost = 0.0
    total_opt_cost = 0.0
    
    for row in schedule:
        print(
            f"| {row['hour']:<17} | {row['buy_price_inr']:>6.2f} | {row['demand_kw']:>7.1f} | "
            f"{row['solar_kw']:>6.1f} | {row['wind_kw']:>6.1f} | {row['charge_kw']:>6.1f} | "
            f"{row['discharge_kw']:>6.1f} | {row['battery_soc_kwh']:>9.1f} | {row['grid_buy_kw']:>6.1f} | "
            f"{row['grid_sell_kw']:>6.1f} | {row['baseline_cost_inr']:>9.2f} | {row['optimized_cost_inr']:>8.2f} |"
        )
        total_base_cost += row["baseline_cost_inr"]
        total_opt_cost += row["optimized_cost_inr"]

    print("=" * 125)
    savings = total_base_cost - total_opt_cost
    savings_pct = (savings / total_base_cost * 100.0) if total_base_cost > 0 else 0.0
    print(f"TOTAL BASELINE COST: {total_base_cost:>15.2f} INR")
    print(f"TOTAL OPTIMIZED COST: {total_opt_cost:>14.2f} INR")
    print(f"NET SAVINGS GENERATED: {savings:>13.2f} INR ({savings_pct:.1f}% cost reduction)")
    print("=" * 125)

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    
    parser = argparse.ArgumentParser(description="GridMind MILP Battery Optimizer CLI")
    parser.add_argument("--sqlite-path", type=str, default="data/gridmind_storage.db", help="Local SQLite file path")
    args = parser.parse_args()

    db = GridMindDBClient(sqlite_path=args.sqlite_path)
    optimizer = GridMindOptimizer(db)
    
    schedule = optimizer.optimize_schedule()
    display_optimal_schedule(schedule)
    
    db.close()
