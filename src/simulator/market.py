import math
import random
from datetime import datetime
from src.simulator.config import (
    GRID_FREQUENCY_NOMINAL, GRID_PRICE_BUY_BASE, GRID_PRICE_SELL_BASE
)

class MarketSimulator:
    def __init__(
        self,
        nominal_freq: float = GRID_FREQUENCY_NOMINAL,
        buy_base: float = GRID_PRICE_BUY_BASE,
        sell_base: float = GRID_PRICE_SELL_BASE
    ):
        self.nominal_freq = nominal_freq
        self.buy_base = buy_base
        self.sell_base = sell_base
        
        # State variables
        self.last_frequency = nominal_freq

    def get_market_state(self, timestamp: datetime) -> dict:
        """
        Calculates electricity buy/sell prices and grid frequency for a given timestamp.
        """
        hour = timestamp.hour
        
        # 1. Price multiplier based on diurnal grid demand profile
        # Peaks: Morning peak (8:00 - 11:00) and Evening peak (18:00 - 21:00)
        # Off-peak: Late night (23:00 - 05:00)
        if 8 <= hour <= 11:
            multiplier = 1.8 + 0.2 * math.sin((hour - 8) / 3.0 * math.pi)
        elif 18 <= hour <= 21:
            multiplier = 2.2 + 0.3 * math.sin((hour - 18) / 3.0 * math.pi)
        elif hour >= 23 or hour <= 5:
            multiplier = 0.6
        else:
            multiplier = 1.0

        # Weekend price reduction (less industrial activity)
        if timestamp.weekday() >= 5:
            multiplier *= 0.85

        # Calculate final prices
        buy_price = self.buy_base * multiplier
        sell_price = self.sell_base * multiplier

        # Add minor random noise (up to 2%) to price
        random.seed(timestamp.timestamp() + 42)
        price_noise = random.uniform(-0.02, 0.02)
        buy_price = max(0.5, buy_price * (1.0 + price_noise))
        sell_price = max(0.3, sell_price * (1.0 + price_noise))

        # Ensure buy price is always greater than sell price
        if sell_price >= buy_price:
            sell_price = buy_price * 0.8

        # 2. Simulate Grid Frequency (normal range: 50.0 +/- 0.05 Hz)
        # Mean reverting behavior towards nominal_freq
        # dF = -k * (F - F_nom) * dt + sigma * dW
        k_reversion = 0.3
        sigma_noise = 0.015
        
        deviation = self.last_frequency - self.nominal_freq
        reversion_delta = -k_reversion * deviation
        
        random_delta = random.uniform(-sigma_noise, sigma_noise)
        
        frequency = self.last_frequency + reversion_delta + random_delta
        # Hard clamp to standard safety limits [49.85 - 50.15]
        frequency = max(self.nominal_freq - 0.15, min(self.nominal_freq + 0.15, frequency))
        self.last_frequency = frequency

        return {
            "buy_price_inr": round(buy_price, 2),
            "sell_price_inr": round(sell_price, 2),
            "grid_frequency_hz": round(frequency, 3)
        }

if __name__ == "__main__":
    market = MarketSimulator()
    now = datetime.now()
    print("Market State Now:")
    print(market.get_market_state(now))
    print("\nMarket State at Midnight:")
    print(market.get_market_state(now.replace(hour=0)))
    print("\nMarket State at 7 PM (Peak):")
    print(market.get_market_state(now.replace(hour=19)))
