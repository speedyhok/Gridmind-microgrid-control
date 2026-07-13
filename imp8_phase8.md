# Phase 8: Bloomberg-Style Premium Web Dashboard (Blue & Green Theme)

**Author**: Mohibul Hoque  
**Email**: [hokworks@gmail.com](mailto:hokworks@gmail.com)  
**LinkedIn**: [linkedin.com/in/speedymohibul](https://linkedin.com/in/speedymohibul)  

This phase implements the user-facing web dashboard of GridMind. It builds a premium, real-time control console using a dark slate canvas accented with neon cyan-blues and emerald-greens, featuring interactive time-series charts (Chart.js) and manual battery overrides.

---

## 1. Design System & Aesthetics
We will construct a dashboard with the following premium design tokens:
* **Background Canvas**: Deep midnight slate (`#0B0F19`) creating a solid, high-contrast command center feel.
* **Card Containers**: Glassmorphic panels with border glows (`rgba(59, 130, 246, 0.1)`) and subtle backdrops.
* **Primary Accent (Cyan-Blue)**: `#00D2FF` / `#2563EB` (representing active grid power, demand, and prices).
* **Secondary Accent (Emerald-Green)**: `#10B981` / `#059669` (representing battery SoC, solar/wind green energy, and cost savings).
* **Typography**: Modern typography imported from Google Fonts (`Outfit` and `Inter`) for a high-tech console layout.

---

## 2. Component Layout & Widgets
* **A. Real-Time Status Header**: Displays system health, database mode, and a ticking clock.
* **B. Hero KPI Grid (glowing circular gauges/cards)**:
  * **Net Grid Load**: Live active bought power (kW) with indicator.
  * **Green Generation**: Solar PV + Wind power output in real-time.
  * **Battery Status**: Current SoC % and active power flow indicator (Charging/Discharging).
  * **Current Prices**: Spot grid pricing (buy vs. sell).
* **C. Interactive Analytics Panels**:
  * **Historical Performance Chart**: Live Chart.js area chart displaying campus demand vs. wind+solar generation.
  * **Optimizer Horizon Chart**: Step chart displaying the optimal battery charge/discharge schedule and predicted grid prices.
* **D. Interactive Override Panel**:
  * Form inputs to trigger manual overrides directly to the API `/api/control/override` (e.g. force charging at 300 kW).

---

## Proposed File Changes

### 1. [NEW] [index.html](file:///c:/Users/Roko/Downloads/DS2/src/api/static/index.html)
Main HTML5 layout. Utilizes CDN scripts for Chart.js and Google Fonts.

### 2. [NEW] [index.css](file:///c:/Users/Roko/Downloads/DS2/src/api/static/index.css)
Declares HSL CSS variables, glassmorphic layout wrappers, keyframe hover effects, and grid positioning.

### 3. [NEW] [app.js](file:///c:/Users/Roko/Downloads/DS2/src/api/static/app.js)
Handles asynchronous fetches to FastAPI, updates DOM metrics, redraws Chart.js instances, and sends battery overrides.

### 4. [MODIFY] [main.py](file:///c:/Users/Roko/Downloads/DS2/src/api/main.py)
Mount static folder using FastAPI `StaticFiles` to serve the dashboard locally at `http://localhost:8000/`.

---

## Verification Plan

### Manual Verification
1. Run the FastAPI backend:
   ```bash
   uv run uvicorn src.api.main:app --reload --port 8000
   ```
2. Open a web browser to `http://localhost:8000/static/index.html` (or `http://localhost:8000/` if mounted as root).
3. Verify that:
   * Charts load historical data and redraw successfully.
   * KPIs update in real-time.
   * Sending a manual override changes the indicator and creates the JSON override config.
