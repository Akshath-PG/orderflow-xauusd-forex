// =====================================================================
//  OrderFlowBridge — cTrader cBot (XAUUSD executor)
//
//  Receives order-flow signals from the Python hub (xauusd/hub.py), which
//  reads big trades / delta / footprint from COMEX gold futures (GC), and
//  trades spot XAUUSD on this account.
//
//  This cBot does all the account work itself: position sizing, prop-firm
//  rules (target, max loss, daily drawdown, minimum profitable days), profit
//  lock, losing-streak cooldown, adaptive risk, TP1 partial + breakeven,
//  time stop, session flatten. Every order has a broker-side stop loss.
//
//  SETUP: run hub.py on the same PC, attach this cBot to an XAUUSD chart,
//  enter your firm's rules in "1. Prop firm rules", pick a Profile.
//  Runs with AccessRights.None. It needs cTrader Desktop on the same machine
//  as the hub (cTrader cloud can't reach 127.0.0.1 — see the setup guide).
// =====================================================================
using System;
using System.Globalization;
using cAlgo.API;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    public enum OfbProfile { Evaluation, Funded, Custom }
    public enum OfbDrawdownType { Static, EndOfDayTrailing, IntradayTrailing }

    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.EasternStandardTime)]
    public class OrderFlowBridge : Robot
    {
        // ---------------- 0. Hub ----------------
        [Parameter("Hub address", DefaultValue = "ws://127.0.0.1:8765", Group = "0. Hub")]
        public string HubUri { get; set; }

        [Parameter("Label (unique per bot)", DefaultValue = "OFB", Group = "0. Hub")]
        public string BotLabel { get; set; }

        // ---------------- 1. Prop firm rules ----------------
        [Parameter("Profile", DefaultValue = OfbProfile.Evaluation, Group = "1. Prop firm rules")]
        public OfbProfile Profile { get; set; }

        [Parameter("Account size ($)", DefaultValue = 5000, MinValue = 100, Group = "1. Prop firm rules")]
        public double AccountSize { get; set; }

        [Parameter("Profit target % (0 = funded)", DefaultValue = 8.0, MinValue = 0, Group = "1. Prop firm rules")]
        public double ProfitTargetPct { get; set; }

        [Parameter("Max loss %", DefaultValue = 8.0, MinValue = 0.1, Group = "1. Prop firm rules")]
        public double MaxLossPct { get; set; }

        [Parameter("Max loss type", DefaultValue = OfbDrawdownType.Static, Group = "1. Prop firm rules")]
        public OfbDrawdownType DrawdownType { get; set; }

        [Parameter("Trailing stops at start balance", DefaultValue = true, Group = "1. Prop firm rules")]
        public bool TrailStopsAtStart { get; set; }

        [Parameter("Daily drawdown % (0 = none)", DefaultValue = 4.0, MinValue = 0, Group = "1. Prop firm rules")]
        public double DailyLossPct { get; set; }

        [Parameter("Daily base = higher of balance/equity", DefaultValue = true, Group = "1. Prop firm rules")]
        public bool DailyBaseHigherOf { get; set; }

        [Parameter("Day resets at (HHMM New York)", DefaultValue = 1700, MinValue = 0, MaxValue = 2359, Group = "1. Prop firm rules")]
        public int DayResetEt { get; set; }

        [Parameter("Min profitable days (0 = none)", DefaultValue = 0, MinValue = 0, Group = "1. Prop firm rules")]
        public int MinProfitableDays { get; set; }

        [Parameter("Profitable day minimum ($)", DefaultValue = 25, MinValue = 0, Group = "1. Prop firm rules")]
        public double ProfitableDayMin { get; set; }

        [Parameter("Max lots (firm cap)", DefaultValue = 5.0, MinValue = 0.01, Group = "1. Prop firm rules")]
        public double MaxLotsFirm { get; set; }

        [Parameter("Safety buffer % (stop before limits)", DefaultValue = 0.8, MinValue = 0, Group = "1. Prop firm rules")]
        public double SafetyBufferPct { get; set; }

        // ---------------- 2. Execution ----------------
        [Parameter("Commission per lot round trip ($)", DefaultValue = 7.0, MinValue = 0, Group = "2. Execution")]
        public double CommissionPerLotRt { get; set; }

        [Parameter("Stop slippage allowance ($ price)", DefaultValue = 0.20, MinValue = 0, Group = "2. Execution")]
        public double StopSlippage { get; set; }

        [Parameter("Flatten at (HHMM New York)", DefaultValue = 1600, MinValue = 0, MaxValue = 2359, Group = "2. Execution")]
        public int FlattenEt { get; set; }

        // ---------------- 3. Bot risk (used by Custom profile) ----------------
        [Parameter("Risk % of room above floor", DefaultValue = 12.0, MinValue = 0.5, Group = "3. Bot risk (Custom)")]
        public double RiskPctOfRoom { get; set; }

        [Parameter("Bot max lots", DefaultValue = 3.0, MinValue = 0.01, Group = "3. Bot risk (Custom)")]
        public double MaxLotsBot { get; set; }

        [Parameter("Use profit lock", DefaultValue = true, Group = "3. Bot risk (Custom)")]
        public bool UseProfitLock { get; set; }

        [Parameter("Profit lock arms at (% of max loss)", DefaultValue = 25.0, MinValue = 1, Group = "3. Bot risk (Custom)")]
        public double LockActivatePctOfDd { get; set; }

        [Parameter("Profit lock giveback %", DefaultValue = 35.0, MinValue = 1, MaxValue = 99, Group = "3. Bot risk (Custom)")]
        public double LockGivebackPct { get; set; }

        [Parameter("Profit lock pause hours", DefaultValue = 24.0, MinValue = 0, Group = "3. Bot risk (Custom)")]
        public double LockPauseHours { get; set; }

        [Parameter("Use streak cooldown", DefaultValue = true, Group = "3. Bot risk (Custom)")]
        public bool UseStreakCooldown { get; set; }

        [Parameter("Max consecutive losses", DefaultValue = 4, MinValue = 1, Group = "3. Bot risk (Custom)")]
        public int MaxConsecutiveLosses { get; set; }

        [Parameter("Streak cooldown hours", DefaultValue = 48.0, MinValue = 0, Group = "3. Bot risk (Custom)")]
        public double StreakCooldownHours { get; set; }

        [Parameter("Use adaptive risk", DefaultValue = true, Group = "3. Bot risk (Custom)")]
        public bool UseAdaptiveRisk { get; set; }

        [Parameter("Risk cut per loss", DefaultValue = 0.70, MinValue = 0.1, MaxValue = 1.0, Group = "3. Bot risk (Custom)")]
        public double RiskCutPerLoss { get; set; }

        [Parameter("Min risk multiplier", DefaultValue = 0.33, MinValue = 0.05, MaxValue = 1.0, Group = "3. Bot risk (Custom)")]
        public double MinRiskMult { get; set; }

        // ---------------- effective settings (after profile) ----------------
        private double riskPct, maxLotsBot, lockActivate, lockGiveback, lockPause, relockBuffer, streakHours;
        private int maxLosses; private bool useLock, useStreak, useAdaptive; private double cutPerLoss, minMult, botDailyStop;

        // ---------------- rule state ----------------
        private double initial, maxDd, target, buffer, floorEq, firmPeak, lockLevel, dailyAllow;
        private double peakEq, lockFloor; private DateTime lockUntil = DateTime.MinValue;
        private int streak; private DateTime streakUntil = DateTime.MinValue;
        private string dead = "";
        private DateTime dayKey = DateTime.MinValue; private double dayStart; private bool dayHalt;
        private int profDays; private bool collect;

        // ---------------- trade state ----------------
        private long tradeId = -1; private int tradeSide; private double tp1Price, tp2Price, bePrice, frac;
        private bool split, tp1Done; private DateTime openedAt; private double maxHoldMin; private double balAtOpen;

        // ---------------- hub link ----------------
        private WebSocketClient ws;
        private DateTime lastMsg;
        private DateTime lastConnectTry = DateTime.MinValue;

        protected override void OnStart()
        {
            ApplyProfile();
            initial = AccountSize;
            maxDd = AccountSize * MaxLossPct / 100.0;
            target = AccountSize * ProfitTargetPct / 100.0;
            buffer = AccountSize * SafetyBufferPct / 100.0;
            floorEq = initial - maxDd;
            firmPeak = initial;
            peakEq = initial;
            lockLevel = TrailStopsAtStart ? initial : double.MaxValue;
            double firmDaily = DailyLossPct > 0 ? AccountSize * DailyLossPct / 100.0 - buffer : 0;
            if (firmDaily > 0 && botDailyStop > 0) dailyAllow = Math.Min(firmDaily, botDailyStop);
            else if (firmDaily > 0) dailyAllow = firmDaily;
            else if (botDailyStop > 0) dailyAllow = botDailyStop;
            else dailyAllow = 0.5 * maxDd;

            Positions.Closed += OnPositionClosed;
            Timer.Start(TimeSpan.FromMilliseconds(500));
            Connect();
            Print(string.Format(CultureInfo.InvariantCulture,
                "[OFB] {0} | {1} | account {2:F0} target {3:F0} max loss {4:F0} ({5}) daily {6:F0} | risk {7}% of room, max {8} lots",
                Profile, SymbolName, AccountSize, target, maxDd, DrawdownType, dailyAllow, riskPct, Math.Min(maxLotsBot, MaxLotsFirm)));
        }

        private void ApplyProfile()
        {
            riskPct = RiskPctOfRoom; maxLotsBot = MaxLotsBot; useLock = UseProfitLock;
            lockActivate = LockActivatePctOfDd / 100.0; lockGiveback = LockGivebackPct; lockPause = LockPauseHours;
            relockBuffer = 0.1; useStreak = UseStreakCooldown; maxLosses = MaxConsecutiveLosses; streakHours = StreakCooldownHours;
            useAdaptive = UseAdaptiveRisk; cutPerLoss = RiskCutPerLoss; minMult = MinRiskMult; botDailyStop = 0;
            double dd = AccountSize * MaxLossPct / 100.0;
            if (Profile == OfbProfile.Evaluation)
            {
                riskPct = 20; maxLotsBot = MaxLotsBot * 1.5; useLock = true; lockActivate = 0.5; lockGiveback = 50; lockPause = 12;
                relockBuffer = 0.2; useStreak = true; maxLosses = 4; streakHours = 24; useAdaptive = true; cutPerLoss = 0.75; minMult = 0.40;
            }
            else if (Profile == OfbProfile.Funded)
            {
                riskPct = 8; maxLotsBot = MaxLotsBot * 0.8; useLock = true; lockActivate = 0.2; lockGiveback = 35; lockPause = 24;
                relockBuffer = 0.1; useStreak = true; maxLosses = 3; streakHours = 48; useAdaptive = true; cutPerLoss = 0.70; minMult = 0.33;
                botDailyStop = 0.2 * dd;
                if (ProfitTargetPct > 0)
                    Print("[OFB] WARNING: Funded profile with a profit target set - the bot will STOP at the target. Set it to 0 for a funded account.");
            }
            lockActivate *= dd;          // fractions of the max loss -> dollars
            relockBuffer *= dd;
        }

        // =================================================================
        //  HUB LINK
        // =================================================================
        private void Connect()
        {
            lastConnectTry = Server.Time;
            try
            {
                if (ws != null) { try { ws.Close(WebSocketClientCloseStatus.NormalClosure); } catch { } }
                ws = new WebSocketClient();
                ws.TextReceived += OnText;
                ws.Connect(new Uri(HubUri));
                lastMsg = Server.Time;
                Print("[OFB] connected to hub " + HubUri);
            }
            catch (Exception e)
            {
                Print("[OFB] hub connection failed: " + e.Message + " - retrying");
            }
        }

        private void OnText(WebSocketClientTextReceivedEventArgs e)
        {
            string text = e.Text;
            BeginInvokeOnMainThread(() => Handle(text));
        }

        private void Handle(string json)
        {
            lastMsg = Server.Time;
            string type = Str(json, "type");
            if (type == "signal") OnSignal(json);
            else if (type == "exit")
            {
                string reason = Str(json, "reason");
                if (tradeId >= 0 && (long)Num(json, "id") == tradeId &&
                    (reason == "flow_reversal" || reason == "time_stop" || reason == "session_end"))
                {
                    Print("[OFB] hub exit #" + tradeId + " (" + reason + ")");
                    CloseAll(reason);
                }
            }
        }

        private void SendStatus(string text)
        {
            try { if (ws != null) ws.Send(text); } catch { }
        }

        // =================================================================
        //  SIGNALS
        // =================================================================
        private void OnSignal(string json)
        {
            long id = (long)Num(json, "id");
            int side = (int)Num(json, "side");
            RulesTick();
            if (MyPosition() != null || tradeId >= 0) { Print("[OFB] signal #" + id + " skipped - already in a position"); return; }
            if (!CanTrade()) { Print("[OFB] signal #" + id + " skipped - rules: " + (dead != "" ? dead : "paused / daily stop / cooldown")); return; }

            double slDist = Num(json, "sl_dist"), tp1Dist = Num(json, "tp1_dist"), tp2Dist = Num(json, "tp2_dist");
            double price = side > 0 ? Symbol.Ask : Symbol.Bid;
            double slPips = (slDist + StopSlippage) / Symbol.PipSize;
            double perUnit = slPips * Symbol.PipValue + CommissionPerLotRt / Symbol.LotSize;
            double units = Units(perUnit);
            if (units < Symbol.VolumeInUnitsMin) { Print("[OFB] signal #" + id + " skipped - risk budget too small for the minimum volume"); return; }

            split = units >= 2 * Symbol.VolumeInUnitsMin;
            double tpDist = split ? tp2Dist : tp1Dist;
            TradeResult r = ExecuteMarketOrder(side > 0 ? TradeType.Buy : TradeType.Sell, SymbolName, units, BotLabel,
                                               slDist / Symbol.PipSize, tpDist / Symbol.PipSize);
            if (!r.IsSuccessful) { Print("[OFB] signal #" + id + " order REJECTED: " + r.Error); return; }

            double entry = r.Position.EntryPrice;
            tradeId = id; tradeSide = side; tp1Done = false;
            tp1Price = entry + side * tp1Dist; tp2Price = entry + side * tpDist;
            bePrice = entry + side * Num(json, "be_offset");
            frac = Num(json, "tp1_fraction");
            maxHoldMin = Num(json, "max_hold_min");
            openedAt = Server.Time;
            balAtOpen = Account.Balance;
            Print(string.Format(CultureInfo.InvariantCulture, "[OFB] OPEN #{0} {1} {2} lots @ {3:F2} SL {4:F2} TP {5:F2} | room {6:F0}",
                id, side > 0 ? "BUY" : "SELL", Symbol.VolumeInUnitsToQuantity(units), entry,
                r.Position.StopLoss ?? 0, r.Position.TakeProfit ?? 0, Room()));
            SendStatus(string.Format(CultureInfo.InvariantCulture,
                "{{\"type\":\"status\",\"executor\":\"ctrader\",\"event\":\"open\",\"id\":{0},\"side\":{1},\"lots\":{2},\"price\":{3:F2}}}",
                id, side, Symbol.VolumeInUnitsToQuantity(units), entry));
        }

        private double Units(double perUnit)
        {
            double room = Room();
            if (room <= 0 || perUnit <= 0) return 0;
            double mult = 1.0;
            if (useAdaptive && streak > 0) mult = Math.Max(minMult, Math.Pow(cutPerLoss, streak));
            if (collect) mult *= 0.25;
            double baseRisk = room * riskPct / 100.0;
            double units = baseRisk * mult / perUnit;
            if (units < Symbol.VolumeInUnitsMin && baseRisk / perUnit >= Symbol.VolumeInUnitsMin)
                units = Symbol.VolumeInUnitsMin;           // adaptive risk never shrinks to zero
            double capLots = Math.Min(maxLotsBot, MaxLotsFirm);
            units = Math.Min(units, Symbol.QuantityToVolumeInUnits(capLots));
            units = Math.Min(units, Symbol.VolumeInUnitsMax);
            units = Symbol.NormalizeVolumeInUnits(units, RoundingMode.Down);
            double eq = Account.Equity, dayPnl = eq - dayStart;
            while (units >= Symbol.VolumeInUnitsMin &&
                   (eq - units * perUnit <= floorEq + buffer || dayPnl - units * perUnit <= -dailyAllow))
                units = Symbol.NormalizeVolumeInUnits(units - Symbol.VolumeInUnitsStep, RoundingMode.Down);
            return units;
        }

        private double Room() { return Account.Equity - (floorEq + buffer); }

        // =================================================================
        //  RULES + TRADE MANAGEMENT
        // =================================================================
        protected override void OnTimer()
        {
            if ((Server.Time - lastMsg).TotalSeconds > 20 && (Server.Time - lastConnectTry).TotalSeconds > 10)
            {
                Print("[OFB] no messages from the hub - reconnecting (open positions keep their stop loss)");
                Connect();
            }
            RulesTick();
            Manage();
        }

        protected override void OnTick()
        {
            Manage();
        }

        private DateTime DayOf(DateTime et)
        {
            int resetMin = (DayResetEt / 100) * 60 + DayResetEt % 100;
            return et.AddMinutes(1440 - resetMin).Date;
        }

        private void RulesTick()
        {
            if (dead != "") return;
            double bal = Account.Balance, eq = Account.Equity;
            DateTime day = DayOf(Server.Time);
            if (day != dayKey)
            {
                if (dayKey != DateTime.MinValue)
                {
                    if (MinProfitableDays > 0 && bal - dayStart >= Math.Max(ProfitableDayMin, 0.01)) profDays++;
                    if (DrawdownType == OfbDrawdownType.EndOfDayTrailing) RaiseFloor(bal);
                }
                dayKey = day; dayStart = DailyBaseHigherOf ? Math.Max(bal, eq) : bal; dayHalt = false;
            }
            if (DrawdownType == OfbDrawdownType.IntradayTrailing && eq > firmPeak) { firmPeak = eq; RaiseFloor(eq); }

            string why = "";
            double dayPnl = eq - dayStart;
            if (eq <= floorEq + buffer)
            { dead = "MAX_LOSS_GUARD"; why = string.Format(CultureInfo.InvariantCulture, "MAX LOSS GUARD at equity {0:F2} (firm floor {1:F2}) - stopped for good", eq, floorEq); }
            else if (target > 0 && eq >= initial + target + 0.25 * buffer)
            {
                bool todayCounts = MinProfitableDays > 0 && dayPnl >= Math.Max(ProfitableDayMin, 0.01);
                if (profDays + (todayCounts ? 1 : 0) >= MinProfitableDays)
                { dead = "PASSED"; why = string.Format(CultureInfo.InvariantCulture, "PROFIT TARGET reached at equity {0:F2} - phase passed", eq); }
                else if (!collect)
                {
                    collect = true;
                    Print(string.Format("[OFB] target reached, {0}/{1} profitable days - trading at quarter size until enough days are banked", profDays, MinProfitableDays));
                }
            }
            if (why == "" && !dayHalt)
            {
                if (dayPnl <= -dailyAllow)
                { dayHalt = true; why = string.Format(CultureInfo.InvariantCulture, "DAILY STOP at day P&L {0:F2}", dayPnl); }
                else if (collect && dayPnl >= Math.Max(ProfitableDayMin, 0.01))
                { dayHalt = true; why = string.Format(CultureInfo.InvariantCulture, "profitable day banked (+{0:F2}) - done for today", dayPnl); }
            }
            if (why == "" && useLock)
            {
                if (eq > peakEq) peakEq = eq;
                double pp = peakEq - initial;
                if (pp >= lockActivate) lockFloor = Math.Max(lockFloor, initial + pp * (1 - lockGiveback / 100.0));
                if (lockFloor > 0 && eq < lockFloor && Server.Time >= lockUntil)
                {
                    lockUntil = Server.Time.AddHours(lockPause);
                    why = string.Format(CultureInfo.InvariantCulture, "PROFIT LOCK at equity {0:F2} (lock {1:F2}) - pausing {2:F0}h", eq, lockFloor, lockPause);
                    lockFloor = eq - relockBuffer;
                    peakEq = eq;
                }
            }
            if (why != "")
            {
                Print("[OFB] " + why);
                CloseAll("rules");
            }
        }

        private void RaiseFloor(double level)
        {
            double cand = Math.Min(level - maxDd, lockLevel);
            if (cand > floorEq) floorEq = cand;
        }

        private bool CanTrade()
        {
            if (dead != "" || dayHalt || Server.Time < lockUntil) return false;
            if (streakUntil != DateTime.MinValue)
            {
                if (Server.Time < streakUntil) return false;
                streakUntil = DateTime.MinValue; streak = 0;
                Print("[OFB] streak cooldown over - trading resumes");
            }
            return true;
        }

        private void Manage()
        {
            Position p = MyPosition();
            if (p == null || tradeId < 0) return;
            double px = tradeSide > 0 ? Symbol.Bid : Symbol.Ask;
            if (split && !tp1Done && tradeSide * (px - tp1Price) >= 0)
            {
                double part = Symbol.NormalizeVolumeInUnits(p.VolumeInUnits * frac, RoundingMode.Down);
                if (part < Symbol.VolumeInUnitsMin) part = Symbol.VolumeInUnitsMin;
                if (part < p.VolumeInUnits)
                {
                    TradeResult r = ClosePosition(p, part);
                    if (r.IsSuccessful)
                    {
                        tp1Done = true;
                        Position rest = MyPosition();
                        if (rest != null) ModifyPosition(rest, bePrice, tp2Price, ProtectionType.Absolute);
                        Print(string.Format(CultureInfo.InvariantCulture, "[OFB] TP1 #{0}: closed part, stop -> breakeven {1:F2}", tradeId, bePrice));
                    }
                }
                return;
            }
            if ((Server.Time - openedAt).TotalMinutes >= maxHoldMin) { Print("[OFB] time stop #" + tradeId); CloseAll("time_stop"); return; }
            int mod = Server.Time.Hour * 60 + Server.Time.Minute;
            int flat = (FlattenEt / 100) * 60 + FlattenEt % 100, reset = (DayResetEt / 100) * 60 + DayResetEt % 100;
            if (mod >= flat && mod < reset) { Print("[OFB] session flatten #" + tradeId); CloseAll("session_end"); }
        }

        private Position MyPosition()
        {
            return Positions.Find(BotLabel, SymbolName);
        }

        private void CloseAll(string reason)
        {
            Position p = MyPosition();
            if (p != null)
            {
                TradeResult r = ClosePosition(p);
                if (!r.IsSuccessful) Print("[OFB] CLOSE FAILED (" + reason + "): " + r.Error + " - broker stop loss still protects the position");
            }
        }

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            if (args.Position.Label != BotLabel || args.Position.SymbolName != SymbolName) return;
            if (MyPosition() != null) return;                       // partial close — trade still open
            double pnl = Account.Balance - balAtOpen;
            if (pnl < -0.01 * maxDd)
            {
                streak++;
                if (useStreak && streak >= maxLosses && streakUntil == DateTime.MinValue)
                {
                    streakUntil = Server.Time.AddHours(streakHours);
                    Print(string.Format("[OFB] {0} losses in a row - cooling down {1:F0}h", streak, streakHours));
                }
            }
            else if (pnl > 0.005 * maxDd) streak = 0;
            Print(string.Format(CultureInfo.InvariantCulture, "[OFB] CLOSED #{0} P&L {1:F2} | balance {2:F2} | floor {3:F2} | streak {4}",
                tradeId, pnl, Account.Balance, floorEq, streak));
            SendStatus(string.Format(CultureInfo.InvariantCulture,
                "{{\"type\":\"status\",\"executor\":\"ctrader\",\"event\":\"closed\",\"id\":{0},\"pnl\":{1:F2},\"balance\":{2:F2}}}",
                tradeId, pnl, Account.Balance));
            tradeId = -1;
        }

        protected override void OnStop()
        {
            try { if (ws != null) ws.Close(WebSocketClientCloseStatus.NormalClosure); } catch { }
        }

        // =================================================================
        //  tiny JSON readers (hub messages are flat key/value objects)
        // =================================================================
        private static string Raw(string json, string key)
        {
            string k = "\"" + key + "\"";
            int i = json.IndexOf(k, StringComparison.Ordinal);
            if (i < 0) return null;
            i = json.IndexOf(':', i + k.Length);
            if (i < 0) return null;
            i++;
            while (i < json.Length && json[i] == ' ') i++;
            int j = i;
            if (j < json.Length && json[j] == '"')
            {
                int end = json.IndexOf('"', j + 1);
                return end < 0 ? null : json.Substring(j + 1, end - j - 1);
            }
            while (j < json.Length && json[j] != ',' && json[j] != '}') j++;
            return json.Substring(i, j - i).Trim();
        }

        private static string Str(string json, string key) { return Raw(json, key) ?? ""; }

        private static double Num(string json, string key)
        {
            string v = Raw(json, key);
            double d;
            return v != null && double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out d) ? d : 0;
        }
    }
}
