// API Endpoint configurations
const API_BASE = "";

// Global Chart References
let historicalChart = null;
let scheduleChart = null;

// AbortController refs — cancels in-flight requests before issuing the next poll
let _kpiAbortCtrl = null;
let _histAbortCtrl = null;
let _schedAbortCtrl = null;

// Clock updates
function startClock() {
    setInterval(() => {
        const now = new Date();
        document.getElementById("live-clock").textContent = now.toTimeString().split(" ")[0];
    }, 1000);
}

// Fetch current status and update DOM KPIs
async function updateRealTimeKPIs() {
    if (_kpiAbortCtrl) _kpiAbortCtrl.abort();
    _kpiAbortCtrl = new AbortController();
    try {
        const response = await fetch(`${API_BASE}/api/status`, { signal: _kpiAbortCtrl.signal });
        if (!response.ok) throw new Error("Status endpoint error");
        
        const data = await response.json();
        
        // 1. Database details
        const dbMode = data.market_pricing ? "POSTGRES" : "SQLITE (Fallback)";
        document.getElementById("db-mode").textContent = dbMode;
        if (data.market_pricing) {
            document.getElementById("db-health-dot").className = "status-dot green";
        }
        
        // 2. Pricing details
        if (data.market_pricing) {
            document.getElementById("kpi-price").textContent = data.market_pricing.buy_price_inr.toFixed(2);
            document.getElementById("kpi-freq").textContent = `Grid Freq: ${data.market_pricing.grid_frequency_hz.toFixed(2)} Hz`;
        }

        // 3. Telemetry assets
        const assets = data.assets || {};
        
        // Campus Demand
        if (assets.campus_aggregate) {
            document.getElementById("kpi-demand").textContent = assets.campus_aggregate.power_kw.toFixed(1);
        }
        
        // Renewables calculations
        const solarKw = assets.solar_pv ? assets.solar_pv.power_kw : 0.0;
        const windKw = assets.wind_turbine ? assets.wind_turbine.power_kw : 0.0;
        const totalGen = solarKw + windKw;
        
        document.getElementById("kpi-generation").textContent = totalGen.toFixed(1);
        document.getElementById("kpi-gen-breakdown").textContent = `Solar: ${solarKw.toFixed(1)} kW | Wind: ${windKw.toFixed(1)} kW`;

        // Battery — power flow direction + live SoC from DB
        if (assets.battery_bank) {
            const power = assets.battery_bank.power_kw;
            const flowText = power > 0 ? `Charging: ${power.toFixed(1)} kW` : (power < 0 ? `Discharging: ${Math.abs(power).toFixed(1)} kW` : "Idle");
            document.getElementById("kpi-battery-flow").textContent = `Voltage: ${assets.battery_bank.voltage || 395}V | ${flowText}`;
            // Update SoC from live DB value (battery_soc_pct stored since Flaw 15 fix)
            if (assets.battery_bank.battery_soc_pct !== null && assets.battery_bank.battery_soc_pct !== undefined) {
                document.getElementById("kpi-soc").textContent = assets.battery_bank.battery_soc_pct.toFixed(1);
            }
        }
        
    } catch (e) {
        console.error("Error updating KPIs:", e);
    }
}

// Load and render Historical Power Flow Chart
async function renderHistoricalChart() {
    if (_histAbortCtrl) _histAbortCtrl.abort();
    _histAbortCtrl = new AbortController();
    try {
        // Query history for the 4 key metrics
        const [solarRes, windRes, demandRes, batteryRes] = await Promise.all([
            fetch(`${API_BASE}/api/metrics/historical?asset_id=solar_pv&limit=30`, { signal: _histAbortCtrl.signal }).then(r => r.json()),
            fetch(`${API_BASE}/api/metrics/historical?asset_id=wind_turbine&limit=30`, { signal: _histAbortCtrl.signal }).then(r => r.json()),
            fetch(`${API_BASE}/api/metrics/historical?asset_id=campus_aggregate&limit=30`, { signal: _histAbortCtrl.signal }).then(r => r.json()),
            fetch(`${API_BASE}/api/metrics/historical?asset_id=battery_bank&limit=30`, { signal: _histAbortCtrl.signal }).then(r => r.json())
        ]);

        const solarData = solarRes.data.reverse();
        const windData = windRes.data.reverse();
        const demandData = demandRes.data.reverse();
        const batteryData = batteryRes.data.reverse();

        // Standardize timestamps labels
        const labels = demandData.map(d => {
            const date = new Date(d.time);
            return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
        });

        const ctx = document.getElementById("historicalChart").getContext("2d");
        
        if (historicalChart) {
            historicalChart.destroy();
        }

        historicalChart = new Chart(ctx, {
            type: "line",
            data: {
                labels: labels,
                datasets: [
                    {
                        label: "Campus Demand (kW)",
                        data: demandData.map(d => d.power_kw),
                        borderColor: "#00D2FF",
                        backgroundColor: "rgba(0, 210, 255, 0.05)",
                        borderWidth: 2,
                        pointRadius: 1,
                        fill: true,
                        tension: 0.3
                    },
                    {
                        label: "Solar Generation (kW)",
                        data: solarData.map(d => d.power_kw),
                        borderColor: "#FBBF24",
                        backgroundColor: "rgba(251, 191, 36, 0.1)",
                        borderWidth: 2,
                        pointRadius: 1,
                        fill: true,
                        tension: 0.3
                    },
                    {
                        label: "Wind Generation (kW)",
                        data: windData.map(d => d.power_kw),
                        borderColor: "#10B981",
                        backgroundColor: "rgba(16, 185, 129, 0.1)",
                        borderWidth: 2,
                        pointRadius: 1,
                        fill: true,
                        tension: 0.3
                    },
                    {
                        label: "Battery Flow (kW)",
                        data: batteryData.map(d => d.power_kw),
                        borderColor: "#6366F1",
                        backgroundColor: "rgba(99, 102, 241, 0.05)",
                        borderWidth: 2,
                        pointRadius: 1,
                        fill: true,
                        tension: 0.3
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        labels: { color: "#334155", font: { family: "Inter", size: 11 } }
                    }
                },
                scales: {
                    x: {
                        grid: { color: "rgba(14, 100, 180, 0.07)" },
                        ticks: { color: "#64748B" }
                    },
                    y: {
                        grid: { color: "rgba(14, 100, 180, 0.07)" },
                        ticks: { color: "#64748B" }
                    }
                }
            }
        });
    } catch (e) {
        if (e.name !== 'AbortError') {
            console.error("Error drawing historical chart:", e);
        }
    }
}

// Load and render Battery Schedule Optimization Chart
async function renderScheduleChart() {
    if (_schedAbortCtrl) _schedAbortCtrl.abort();
    _schedAbortCtrl = new AbortController();
    try {
        const response = await fetch(`${API_BASE}/api/schedule`, { signal: _schedAbortCtrl.signal });
        if (!response.ok) {
            document.getElementById("savings-badge").textContent = "No Schedule Prediction Data";
            return;
        }

        const schedule = await response.json();
        
        // Update Savings Badge
        let totalBase = 0;
        let totalOpt = 0;
        schedule.forEach(r => {
            totalBase += r.baseline_cost_inr;
            totalOpt += r.optimized_cost_inr;
        });
        const savings = totalBase - totalOpt;
        const pct = totalBase > 0 ? (savings / totalBase * 100.0) : 0.0;
        document.getElementById("savings-badge").textContent = `Savings: ${pct.toFixed(1)}% (${savings.toFixed(0)} INR)`;

        // Standardize labels
        const labels = schedule.map(r => {
            const date = new Date(r.hour);
            return `${String(date.getHours()).padStart(2, '0')}:00`;
        });

        // Set Battery SoC for real-time KPI card display
        // Don't update kpi-soc here — SoC comes from live /api/status (battery_soc_pct column)
        // to prevent the schedule's optimizer SoC from overwriting the real live value.

        const ctx = document.getElementById("scheduleChart").getContext("2d");
        
        if (scheduleChart) {
            scheduleChart.destroy();
        }

        // Map positive values for charging and negative values for discharging
        const netFlows = schedule.map(r => r.charge_kw > 0 ? r.charge_kw : -r.discharge_kw);

        scheduleChart = new Chart(ctx, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [
                    {
                        label: "Optimized Battery Flow (kW)",
                        data: netFlows,
                        backgroundColor: netFlows.map(v => v >= 0 ? "rgba(16, 185, 129, 0.7)" : "rgba(239, 68, 68, 0.7)"),
                        borderColor: netFlows.map(v => v >= 0 ? "#10B981" : "#EF4444"),
                        borderWidth: 1,
                        yAxisID: "y"
                    },
                    {
                        label: "Battery SoC (kWh)",
                        type: "line",
                        data: schedule.map(r => r.battery_soc_kwh),
                        borderColor: "#34D399",
                        backgroundColor: "rgba(52, 211, 153, 0.05)",
                        borderWidth: 2,
                        pointRadius: 2,
                        fill: true,
                        yAxisID: "y_soc"
                    },
                    {
                        label: "Buy Price (INR/kWh)",
                        type: "line",
                        data: schedule.map(r => r.buy_price_inr),
                        borderColor: "#818CF8",
                        borderWidth: 1.5,
                        pointRadius: 0,
                        fill: false,
                        yAxisID: "y_price"
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        labels: { color: "#334155", font: { family: "Inter", size: 11 } }
                    }
                },
                scales: {
                    x: {
                        grid: { color: "rgba(14, 100, 180, 0.07)" },
                        ticks: { color: "#64748B" }
                    },
                    y: {
                        title: { display: true, text: "Battery Flow (kW)", color: "#64748B" },
                        grid: { color: "rgba(14, 100, 180, 0.07)" },
                        ticks: { color: "#64748B" }
                    },
                    y_soc: {
                        position: "right",
                        title: { display: true, text: "SoC (kWh)", color: "#64748B" },
                        grid: { drawOnChartArea: false },
                        ticks: { color: "#64748B" },
                        min: 0,
                        max: 2000
                    },
                    y_price: {
                        position: "right",
                        title: { display: false },
                        grid: { drawOnChartArea: false },
                        ticks: { display: false }
                    }
                }
            }
        });

    } catch (e) {
        if (e.name !== 'AbortError') {
            console.error("Error drawing schedule chart:", e);
        }
    }
}

// UI overrides Form control
const overrideCmdSelect = document.getElementById("override-cmd");
const rateGroup = document.getElementById("rate-group");
const overrideRateInput = document.getElementById("override-rate");
const rangeValLbl = document.getElementById("range-val-lbl");
const overrideForm = document.getElementById("override-form");

// Toggle slider group based on override action selection
overrideCmdSelect.addEventListener("change", (e) => {
    if (e.target.value === "none") {
        rateGroup.style.display = "none";
    } else {
        rateGroup.style.display = "block";
    }
});

// Update slider value label dynamically
overrideRateInput.addEventListener("input", (e) => {
    rangeValLbl.textContent = `${e.target.value} kW`;
});

// Submit manual overrides
overrideForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    
    const command = overrideCmdSelect.value;
    const rateKw = parseFloat(overrideRateInput.value);
    const btnSubmit = document.getElementById("btn-submit-override");
    
    // Disable button while running
    btnSubmit.textContent = "Applying...";
    btnSubmit.disabled = true;

    try {
        const headers = { 
            "Content-Type": "application/json",
            "X-API-Key": "gridmind_premium_secret_key"
        };
        
        // Step 1: Write the override file
        const overrideRes = await fetch(`${API_BASE}/api/control/override`, {
            method: "POST",
            headers: headers,
            body: JSON.stringify({ command, rate_kw: command === "none" ? 0.0 : rateKw })
        });
        
        if (!overrideRes.ok) throw new Error("Override API error");
        
        // Step 2: Trigger a live simulation tick with the new override
        const liveRes = await fetch(`${API_BASE}/api/live-update`, {
            method: "POST",
            headers: { "X-API-Key": "gridmind_premium_secret_key" }
        });
        
        if (liveRes.ok) {
            const live = await liveRes.json();
            
            // Immediately update KPI cards from the live tick response
            document.getElementById("kpi-demand").textContent = live.demand_kw.toFixed(1);
            document.getElementById("kpi-generation").textContent = (live.solar_kw + live.wind_kw).toFixed(1);
            document.getElementById("kpi-gen-breakdown").textContent = `Solar: ${live.solar_kw.toFixed(1)} kW | Wind: ${live.wind_kw.toFixed(1)} kW`;
            document.getElementById("kpi-soc").textContent = live.battery_soc_pct.toFixed(0);
            document.getElementById("kpi-price").textContent = live.buy_price_inr.toFixed(2);
            
            const flowDir = live.battery_power_kw > 0 ? `Charging: ${live.battery_power_kw.toFixed(1)} kW`
                          : live.battery_power_kw < 0 ? `Discharging: ${Math.abs(live.battery_power_kw).toFixed(1)} kW`
                          : "Idle";
            document.getElementById("kpi-battery-flow").textContent = `SoC: ${live.battery_soc_pct.toFixed(1)}% | ${flowDir}`;

            // Refresh historical chart to include the new DB row
            renderHistoricalChart();
        }
        
        // Step 3: Update UI override status tag and alert
        const tag = document.getElementById("override-status-tag");
        const alertEl = document.getElementById("override-active-alert");
        
        if (command === "none") {
            tag.textContent = "Optimizer Control";
            tag.className = "status-tag";
            alertEl.style.display = "none";
        } else {
            tag.textContent = `Override: ${command.toUpperCase()} @ ${rateKw} kW`;
            tag.className = "status-tag active-override";
            alertEl.style.display = "block";
        }
        
    } catch (err) {
        console.error("Error submitting manual override:", err);
    } finally {
        btnSubmit.textContent = "Apply Command";
        btnSubmit.disabled = false;
    }
});

// Startup Execution
startClock();
updateRealTimeKPIs();
renderHistoricalChart();
renderScheduleChart();

// Polling interval loops
setInterval(updateRealTimeKPIs, 5000);
setInterval(renderHistoricalChart, 15000);
setInterval(renderScheduleChart, 30000);
