def parse_discrete_credits(raw_credits):
    """
    Takes an array of raw cumulative credits from IPFS, 
    groups them by device_id, sorts chronologically, and computes
    discrete delta values (clamped to >= 0) for each credit.
    Returns a flat list of parsed discrete credits, sorted by credit_id.
    """
    if not raw_credits:
        return []

    # Group by device_id
    grouped = {}
    for c in raw_credits:
        device_id = c.get("device_id", "unknown")
        if device_id not in grouped:
            grouped[device_id] = []
        grouped[device_id].append(c)
    
    parsed = []
    
    for device_id, group in grouped.items():
        # Sort strictly by timestamp within each device
        group.sort(key=lambda x: x.get("timestamp", ""))
        
        prev_kwh = 0.0
        prev_co2 = 0.0
        
        for c in group:
            cum_kwh = float(c.get("total_kwh", 0))
            cum_co2 = float(c.get("co2_avoided_kg", 0))
            
            # Compute delta and clamp to 0
            delta_kwh = max(0.0, cum_kwh - prev_kwh)
            delta_co2 = max(0.0, cum_co2 - prev_co2)
            
            prev_kwh = cum_kwh
            prev_co2 = cum_co2
            
            # Create a copy so we don't mutate the original if it's cached differently
            parsed_c = c.copy()
            parsed_c["total_kwh"] = delta_kwh
            parsed_c["co2_avoided_kg"] = delta_co2
            parsed.append(parsed_c)
            
    # Return the full list sorted back by credit_id
    parsed.sort(key=lambda x: x.get("credit_id", 0))
    return parsed
