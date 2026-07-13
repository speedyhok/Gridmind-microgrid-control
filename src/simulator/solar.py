from src.simulator.config import (
    SOLAR_EFFICIENCY, SOLAR_AREA, TEMP_COEFF, REF_TEMP, RADIATION_HEAT_COEFF
)

class SolarSimulator:
    def __init__(
        self,
        efficiency: float = SOLAR_EFFICIENCY,
        area: float = SOLAR_AREA,
        temp_coeff: float = TEMP_COEFF,
        ref_temp: float = REF_TEMP,
        rad_heat_coeff: float = RADIATION_HEAT_COEFF
    ):
        self.eta = efficiency
        self.A = area
        self.beta = temp_coeff
        self.T_ref = ref_temp
        self.gamma = rad_heat_coeff

    def calculate_power(self, irradiance: float, ambient_temp: float) -> float:
        """
        Calculates Solar PV power output in kW based on weather conditions.
        Formula:
            P_solar = eta * A * I(t) * (1 - beta * (T_cell - T_ref)) / 1000
            where T_cell = T_ambient + gamma * I(t)
        """
        if irradiance <= 0:
            return 0.0

        # Calculate cell temperature based on ambient temperature and solar heating
        t_cell = ambient_temp + self.gamma * irradiance
        
        # Calculate temperature loss coefficient
        temp_loss = 1.0 - self.beta * (t_cell - self.T_ref)
        
        # Ensure loss doesn't completely invert the production (keep it non-negative)
        temp_loss = max(0.0, temp_loss)
        
        # Calculate output in Watts, then convert to kW
        power_w = self.eta * self.A * irradiance * temp_loss
        power_kw = power_w / 1000.0
        
        return round(power_kw, 3)

if __name__ == "__main__":
    solar = SolarSimulator()
    # Test cases
    print("Noon sun (1000 W/m2, 25C):", solar.calculate_power(1000.0, 25.0), "kW")
    print("Hot noon sun (1000 W/m2, 40C):", solar.calculate_power(1000.0, 40.0), "kW")
    print("Night (0 W/m2, 15C):", solar.calculate_power(0.0, 15.0), "kW")
