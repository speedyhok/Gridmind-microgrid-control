from src.simulator.config import (
    BATTERY_CAPACITY_KWH, BATTERY_MAX_CHARGE_KW, BATTERY_MAX_DISCHARGE_KW,
    BATTERY_EFFICIENCY, BATTERY_MIN_SOC, BATTERY_MAX_SOC, BATTERY_DEGRADATION_PER_CYCLE
)

class BatterySimulator:
    def __init__(
        self,
        capacity_kwh: float = BATTERY_CAPACITY_KWH,
        max_charge_kw: float = BATTERY_MAX_CHARGE_KW,
        max_discharge_kw: float = BATTERY_MAX_DISCHARGE_KW,
        efficiency: float = BATTERY_EFFICIENCY,
        min_soc: float = BATTERY_MIN_SOC,
        max_soc: float = BATTERY_MAX_SOC,
        deg_per_cycle: float = BATTERY_DEGRADATION_PER_CYCLE
    ):
        self.nominal_capacity = capacity_kwh
        self.max_charge = max_charge_kw
        self.max_discharge = max_discharge_kw
        self.efficiency = efficiency  # eta_c = eta_d
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.deg_per_cycle = deg_per_cycle

        # State variables
        self.soc = 50.0        # State of Charge (%)
        self.soh = 100.0       # State of Health (%)
        self.cell_temp = 25.0  # Cell temperature (°C)
        self.equivalent_cycles = 0.0
        self.nominal_voltage = 400.0 # Volts

    def step(self, charge_demand_kw: float, discharge_demand_kw: float, ambient_temp: float, dt_hours: float) -> dict:
        """
        Advances the battery simulation state by dt_hours.
        Returns telemetry parameters: power_kw, voltage, cell_temp, soc, soh, anomaly_flag.
        """
        # Calculate current actual capacity in kWh based on degradation
        current_capacity = self.nominal_capacity * (self.soh / 100.0)

        # 1. Determine actual charge/discharge rates, respecting constraints
        actual_charge_kw = 0.0
        actual_discharge_kw = 0.0
        power_kw = 0.0

        if charge_demand_kw > 0 and discharge_demand_kw == 0:
            # Charging
            limit_by_power = min(charge_demand_kw, self.max_charge)
            # Limit by maximum SOC
            available_room_kwh = max(0.0, (self.max_soc - self.soc) / 100.0 * current_capacity)
            limit_by_soc = (available_room_kwh / self.efficiency) / dt_hours if dt_hours > 0 else 0.0
            actual_charge_kw = min(limit_by_power, limit_by_soc)
            power_kw = actual_charge_kw  # Positive power is charging/grid-buying in optimization, wait, let's treat net power out as negative, charging as positive
        elif discharge_demand_kw > 0 and charge_demand_kw == 0:
            # Discharging
            limit_by_power = min(discharge_demand_kw, self.max_discharge)
            # Limit by minimum SOC
            available_energy_kwh = max(0.0, (self.soc - self.min_soc) / 100.0 * current_capacity)
            limit_by_soc = (available_energy_kwh * self.efficiency) / dt_hours if dt_hours > 0 else 0.0
            actual_discharge_kw = min(limit_by_power, limit_by_soc)
            power_kw = -actual_discharge_kw # Negative power represents discharging/supplying

        # 2. Update State of Charge (SOC)
        net_energy_delta = (actual_charge_kw * self.efficiency - (actual_discharge_kw / self.efficiency)) * dt_hours
        if current_capacity > 0:
            self.soc += (net_energy_delta / current_capacity) * 100.0
        self.soc = max(0.0, min(100.0, self.soc))

        # 3. Simulate Cell Temperature
        # Internal heating from losses: P_loss = (1 - efficiency) * |P_flow|
        p_flow = actual_charge_kw + actual_discharge_kw
        heat_generated = (1.0 - self.efficiency) * p_flow * 0.1  # Thermal coefficient scaling
        # Thermal cooling/conduction to ambient: Q_cool = K_thermal * (T_cell - T_ambient)
        cooling_rate = 0.15 * (self.cell_temp - ambient_temp)
        
        # Temp delta = heat_gen - cooling
        self.cell_temp += (heat_generated - cooling_rate) * dt_hours
        
        # 4. Simulate Voltage Drop (Internal Resistance / polarization effect)
        # C-rate = power / capacity
        c_rate = p_flow / current_capacity if current_capacity > 0 else 0.0
        # Voltage drop/rise: Delta V = I * R ~ C-rate * R_eff
        voltage_delta = c_rate * 30.0  # Resistance scaling factor
        if actual_charge_kw > 0:
            voltage = self.nominal_voltage + voltage_delta
        elif actual_discharge_kw > 0:
            voltage = self.nominal_voltage - voltage_delta
        else:
            # Open circuit voltage varies slightly with SOC
            voltage = self.nominal_voltage + (self.soc - 50.0) * 0.4

        # 5. Health Degradation (Capacity Fade)
        # Equivalent cycles added
        flow_kwh = (actual_charge_kw + actual_discharge_kw) * dt_hours
        cycle_increment = flow_kwh / (2.0 * current_capacity) if current_capacity > 0 else 0.0
        self.equivalent_cycles += cycle_increment

        # Degradation acceleration factor due to cell temperature (Arrhenius-like approximation)
        temp_stress_factor = 1.0
        if self.cell_temp > 35.0:
            temp_stress_factor = 1.0 + (self.cell_temp - 35.0) * 0.1
        elif self.cell_temp > 50.0:
            temp_stress_factor = 2.5 + (self.cell_temp - 50.0) * 0.5

        # Capacity fade rate per equivalent cycle
        soh_loss = cycle_increment * self.deg_per_cycle * temp_stress_factor * 100.0
        self.soh = max(0.0, self.soh - soh_loss)

        # 6. Safety Flags (Anomaly checking)
        anomaly_flag = False
        if self.cell_temp > 60.0 or self.soc < 5.0 or self.soc > 95.0 or self.soh < 40.0:
            anomaly_flag = True

        return {
            "power_kw": round(power_kw, 3),
            "voltage": round(voltage, 1),
            "cell_temp": round(self.cell_temp, 2),
            "soc": round(self.soc, 2),
            "soh": round(self.soh, 3),
            "cycles": round(self.equivalent_cycles, 4),
            "anomaly_flag": anomaly_flag
        }

if __name__ == "__main__":
    batt = BatterySimulator()
    print("Initial step (Idle, Ambient 25C):")
    print(batt.step(0, 0, 25.0, 1.0))
    print("\nCharging high rate (500 kW, Ambient 25C) for 1 hour:")
    print(batt.step(500.0, 0.0, 25.0, 1.0))
    print("\nDischarging high rate (500 kW, Ambient 25C) for 1 hour:")
    print(batt.step(0.0, 500.0, 25.0, 1.0))
