import math
import random
from datetime import datetime
from src.simulator.config import BUILDINGS_CONFIG

class CampusMetersSimulator:
    def __init__(self, buildings_config: dict = BUILDINGS_CONFIG):
        self.configs = buildings_config

    def _get_profile_factor(self, b_type: str, hour: int, weekday: bool) -> float:
        """
        Returns a scaling factor [0.0 - 1.0] for the load profile based on hour and day type.
        """
        # Weekend load factor reduction
        weekend_scale = 1.0 if weekday else 0.4

        if b_type == "academic":
            # Peak during class hours (8:00 - 18:00)
            if 8 <= hour <= 17:
                factor = 0.8 + 0.2 * math.sin((hour - 8) / 10.0 * math.pi)
            elif 18 <= hour <= 22:
                factor = 0.3 + 0.2 * ((22 - hour) / 4.0)
            else:
                factor = 0.15
            return factor * weekend_scale

        elif b_type == "industrial":  # Lab loads
            # High baseload, slight daytime increase
            if 9 <= hour <= 17:
                factor = 0.9
            else:
                factor = 0.65
            return factor * (1.0 if weekday else 0.7)

        elif b_type == "residential":
            # Dual peak: Morning prep (7:00 - 9:00) & Evening (17:00 - 23:00)
            if 6 <= hour <= 9:
                factor = 0.5 + 0.45 * math.sin((hour - 6) / 3.0 * math.pi / 2)
            elif 17 <= hour <= 23:
                factor = 0.6 + 0.38 * math.sin((hour - 17) / 6.0 * math.pi)
            elif 10 <= hour <= 16:
                factor = 0.25  # Empty during the day
            else:
                factor = 0.15  # Sleeping hours
            # Higher residential load on weekends
            return factor * (1.0 if weekday else 1.2)

        elif b_type == "commercial":  # Dining/Gym
            # Dining halls peak at breakfast (7-9), lunch (12-14), dinner (18-20)
            if 7 <= hour <= 9:
                factor = 0.7
            elif 11 <= hour <= 14:
                factor = 0.95
            elif 17 <= hour <= 20:
                factor = 0.9
            else:
                factor = 0.2
            return factor * weekend_scale

        elif b_type == "ev":
            # Charging peaks when cars arrive: morning work arrival (8-10) and evening home arrival (17-19)
            if 8 <= hour <= 10:
                factor = 0.85
            elif 17 <= hour <= 19:
                factor = 0.75
            elif 11 <= hour <= 16:
                factor = 0.3
            else:
                factor = 0.05
            return factor * weekend_scale

        return 0.5

    def get_building_load(self, name: str, timestamp: datetime) -> dict:
        """
        Calculates active and reactive load for a specific building at a given timestamp.
        """
        config = self.configs.get(name)
        if not config:
            raise ValueError(f"Building {name} not found in configuration.")

        b_type = config["type"]
        peak_kw = config["peak_kw"]
        pf = config["power_factor"]

        # Get base hourly profile factor
        hour = timestamp.hour
        weekday = timestamp.weekday() < 5
        base_factor = self._get_profile_factor(b_type, hour, weekday)

        # Add 5% random noise to simulate micro-fluctuations
        random.seed(timestamp.timestamp() + hash(name))
        noise = random.uniform(-0.05, 0.05)
        active_power = peak_kw * max(0.05, base_factor + noise)

        # Calculate reactive power: Q = P * tan(theta)
        # pf = cos(theta) -> tan(theta) = sqrt(1 - pf^2) / pf
        tan_theta = math.sqrt(1.0 - pf**2) / pf
        reactive_power = active_power * tan_theta

        return {
            "building_id": name,
            "building_type": b_type,
            "active_power_kw": round(active_power, 3),
            "reactive_power_kvar": round(reactive_power, 3),
            "apparent_power_kva": round(active_power / pf, 3),
            "power_factor": pf
        }

    def get_total_campus_load(self, timestamp: datetime) -> dict:
        """
        Aggregates loads across all campus buildings.
        """
        total_p = 0.0
        total_q = 0.0
        details = {}

        for b_name in self.configs:
            load = self.get_building_load(b_name, timestamp)
            total_p += load["active_power_kw"]
            total_q += load["reactive_power_kvar"]
            details[b_name] = load

        # Aggregated power factor
        if total_p > 0:
            total_s = math.sqrt(total_p**2 + total_q**2)
            agg_pf = total_p / total_s
        else:
            total_s = 0.0
            agg_pf = 1.0

        return {
            "total_active_power_kw": round(total_p, 3),
            "total_reactive_power_kvar": round(total_q, 3),
            "total_apparent_power_kva": round(total_s, 3),
            "aggregate_power_factor": round(agg_pf, 3),
            "breakdown": details
        }

if __name__ == "__main__":
    sim = CampusMetersSimulator()
    now = datetime.now()
    print("Aggregate Campus Load Now:")
    load_now = sim.get_total_campus_load(now)
    print(f"Active: {load_now['total_active_power_kw']} kW, PF: {load_now['aggregate_power_factor']}")
    print("\nAcademic_1 Load breakdown:")
    print(load_now["breakdown"]["Academic_1"])
