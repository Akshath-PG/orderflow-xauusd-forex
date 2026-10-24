"""Risk profiles. They set only the BOT's own risk knobs — the firm rules you
entered in [propfirm] (account size, drawdown, daily limit, target, contract
cap) are never changed by a profile.

  evaluation — pass fast. Bigger positions, looser profit lock, shorter
               cooldown. Failing an evaluation costs a reset fee, so more
               risk is acceptable here.
  funded     — keep the account. Smaller positions, tighter daily stop,
               cooldown after 3 losses. Aim: steady ~2%/month.
  custom     — use the [risk] values exactly as written.
"""

PROFILES = {
    "evaluation": {
        "risk": {
            "risk_pct_of_room": 20.0,      # ~ $360 per trade on $1,800 of room
            "use_profit_lock": True,
            "profit_lock_giveback_pct": 50.0,
            "profit_lock_pause_hours": 12.0,
            "use_streak_cooldown": True,
            "max_consecutive_losses": 4,
            "streak_cooldown_hours": 24.0,
            "use_adaptive_risk": True,
            "risk_cut_per_loss": 0.75,
            "min_risk_mult": 0.40,
        },
        "strategy": {"cooldown_bars": 2},
        "max_contracts_mult": 1.5,         # 1.5x the config's own cap
        "lock_activate_x_dd": 0.5,         # profit lock arms at 50% of the drawdown in profit
        "relock_x_dd": 0.2,
        "bot_daily_stop_x_dd": 0.0,        # firm daily limit minus buffer
    },
    "funded": {
        "risk": {
            "risk_pct_of_room": 8.0,       # ~ $145 per trade on $1,800 of room
            "use_profit_lock": True,
            "profit_lock_giveback_pct": 35.0,
            "profit_lock_pause_hours": 24.0,
            "use_streak_cooldown": True,
            "max_consecutive_losses": 3,
            "streak_cooldown_hours": 48.0,
            "use_adaptive_risk": True,
            "risk_cut_per_loss": 0.70,
            "min_risk_mult": 0.33,
        },
        "strategy": {"cooldown_bars": 3},
        "max_contracts_mult": 0.8,         # 0.8x the config's own cap
        "lock_activate_x_dd": 0.2,         # arms at 20% of the drawdown in profit
        "relock_x_dd": 0.1,
        "bot_daily_stop_x_dd": 0.20,       # bot stops the day at 20% of the drawdown ($400 on $2,000)
    },
}


def apply_profile(cfg: dict) -> str:
    """Apply cfg['risk']['profile'] in place. Returns the profile name."""
    name = str(cfg["risk"].get("profile", "custom")).lower()
    if name == "custom":
        return name
    if name not in PROFILES:
        raise ValueError(f"profile must be evaluation, funded or custom - got {name!r}")
    p = PROFILES[name]
    cfg["risk"].update(p["risk"])
    # contract units differ by market (futures contracts vs 0.01-lot units), so scale the config's cap
    cfg["risk"]["max_contracts"] = max(1, int(cfg["risk"]["max_contracts"] * p["max_contracts_mult"]))
    cfg["strategy"].update(p["strategy"])
    dd = cfg["propfirm"]["max_drawdown"]           # dollar thresholds scale with the account's drawdown
    cfg["risk"]["profit_lock_activate"] = p["lock_activate_x_dd"] * dd
    cfg["risk"]["relock_buffer"] = p["relock_x_dd"] * dd
    if p["bot_daily_stop_x_dd"] > 0:
        cfg["propfirm"]["bot_daily_stop"] = p["bot_daily_stop_x_dd"] * cfg["propfirm"]["max_drawdown"]
    if name == "funded" and cfg["propfirm"]["profit_target"] > 0:
        print("WARNING: funded profile with a profit target set - the bot will STOP at the target. "
              "Set profit_target = 0 for a funded account.")
    return name
