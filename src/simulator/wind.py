import random
from src.simulator.config import (
    WIND_CUT_IN, WIND_RATED, WIND_CUT_OUT, AIR_DENSITY,
    ROTOR_AREA, POWER_COEFF, WIND_RATED_POWER
)

class WindTurbineSimulator:
    def __init__(
        self,
        cut_in: float = WIND_CUT_IN,
        rated: float = WIND_RATED,
        cut_out: float = WIND_CUT_OUT,
        rho: float = AIR_DENSITY,
        area: float = ROTOR_AREA,
        cp: float = POWER_COEFF,
        rated_power: float = WIND_RATED_POWER
    ):
        self.v_cut_in = cut_in
        self.v_rated = rated
        self.v_cut_out = cut_out
        self.rho = rho
        self.A = area
        self.C_p = cp
        self.P_rated = rated_power
        
        # State variables
        self.vibration_index = 1.0  # Base normal vibration index (1.0 is healthy)

    def calculate_power_and_wear(self, wind_speed: float, timestamp_sec: float) -> tuple[float, float]:
        """
        Calculates Wind Turbine power output in kW and updates vibration index.
        Formula:
            P_wind = 0, for v < v_cut_in or v >= v_cut_out
            P_wind = 0.5 * rho * A * v^3 * Cp / 1000, for v_cut_in <= v < v_rated
            P_wind = P_rated, for v_rated <= v < v_cut_out
        """
        # Determine base power output
        if wind_speed < self.v_cut_in or wind_speed >= self.v_cut_out:
            power_kw = 0.0
        elif self.v_cut_in <= wind_speed < self.v_rated:
            # Calculate aerodynamic power in Watts and convert to kW
            power_w = 0.5 * self.rho * self.A * (wind_speed ** 3) * self.C_p
            power_kw = min(power_w / 1000.0, self.P_rated)
        else:
            power_kw = self.P_rated

        # Simulate mechanical wear (rolling vibration index)
        # Vibration increases with high wind speeds and random turbulence
        random.seed(timestamp_sec)
        turbulence = random.uniform(-0.05, 0.05)
        
        if wind_speed >= self.v_cut_out:
            # Turbine is parked, vibration drops/stabilizes
            target_vibration = 0.8
        elif wind_speed >= self.v_rated:
            # Operating at rated speed (high stress)
            target_vibration = 1.8 + turbulence
        else:
            # Normal operation
            target_vibration = 1.0 + (wind_speed / self.v_rated) * 0.5 + turbulence

        # Smooth vibration transition (rolling average/filter)
        self.vibration_index = 0.95 * self.vibration_index + 0.05 * target_vibration
        
        return round(power_kw, 3), round(self.vibration_index, 3)

if __name__ == "__main__":
    turbine = WindTurbineSimulator()
    print("Wind Speed: 2 m/s -> Power:", turbine.calculate_power_and_wear(2.0, 100)[0], "kW")
    print("Wind Speed: 8 m/s -> Power:", turbine.calculate_power_and_wear(8.0, 101)[0], "kW")
    print("Wind Speed: 12 m/s -> Power:", turbine.calculate_power_and_wear(12.0, 102)[0], "kW")
    print("Wind Speed: 20 m/s -> Power:", turbine.calculate_power_and_wear(20.0, 103)[0], "kW")
    print("Wind Speed: 26 m/s -> Power:", turbine.calculate_power_and_wear(26.0, 104)[0], "kW")
