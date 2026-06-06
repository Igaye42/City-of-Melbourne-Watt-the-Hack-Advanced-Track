
 
 
class Strategy:
    """Duck-curve controller: bank the midday solar belly, spend it on the
    evening price peak. Purely reactive to current telemetry (no forecast
    is available on this scenario)."""
 
   
    CAP_MWH = 100.0        # battery_capacity_mwh
    INV_MW = 50.0          # max_inverter_mw
    CHARGE_EFF = 0.95      # charge_efficiency
    DISCHARGE_EFF = 0.95   # discharge_efficiency
    DT_H = 0.25            # 15-minute step
    EXPORT_CAP_MW = 50.0   # grid_max_export_mw (beyond this -> overvoltage)
    IMPORT_CAP_MW = 120.0  # grid_max_import_mw (beyond this -> blackout)
 
    
    PEAK_PRICE = 450.0
 
    def _feasible(self, flow_mw: float, soc: float) -> float:
        """Clip a requested battery flow the way the engine will:
        + = discharge, - = charge. Bounded by the inverter and by the energy
        the battery can actually source (discharge) or absorb (charge)."""
        if flow_mw < 0.0:  # charging
            max_charge = (1.0 - soc) * self.CAP_MWH / (self.CHARGE_EFF * self.DT_H)
            return -min(-flow_mw, self.INV_MW, max_charge)
        if flow_mw > 0.0:  # discharging
            max_discharge = soc * self.CAP_MWH * self.DISCHARGE_EFF / self.DT_H
            return min(flow_mw, self.INV_MW, max_discharge)
        return 0.0
 
    def step(self, state):
        demand = float(state.get("demand", 0.0))
        solar = float(state.get("solar", 0.0))
        soc = float(state.get("soc", 0.0))
        price = float(state.get("price", 0.0))
 
        net = demand - solar  # +ve = local shortfall, -ve = solar surplus
 
        if net < 0.0:
            # Midday belly: store the entire surplus. _feasible() caps it at
            # the inverter / remaining headroom; whatever the battery can't
            # take spills to the grid and is handled by curtailment below.
            flow = net
        elif price >= self.PEAK_PRICE:
            # Evening neck: discharge to cover the shortfall (never more than
            # the deficit — exporting battery energy only earns the $50/MWh
            # tariff, far less than the import it offsets).
            flow = net
        else:
            # Cheap overnight / shoulder hours: hold charge for the peak.
            flow = 0.0
 
        flow = self._feasible(flow, soc)
 
        # Base curtailment and diesel on the REALIZED flow.
        net_after = demand - solar - flow
        curtail = max(0.0, -net_after - self.EXPORT_CAP_MW)   # trim exports to 50 MW
        generator = max(0.0, net_after - self.IMPORT_CAP_MW)  # diesel only to avoid blackout
 
        return {
            "battery_flow_mw": flow,
            "curtail_solar": curtail,
            "emergency_generator": generator,
            "fcas_reserve_mw": 0.0,  # FCAS is disabled on the duck curve
        }
 
 
# --- Local playtest. Runs on `python strategy.py`; the judge ignores this. ---
if __name__ == "__main__":
    from watt_the_hack.playtest import run_playtest
 
    result = run_playtest(__file__, "duck_curve", plots=True, open_report=True)
    print(f"\nRaw cost (lower wins): ${result['metrics']['final_score']:,.2f}")