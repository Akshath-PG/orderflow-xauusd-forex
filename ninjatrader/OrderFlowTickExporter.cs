#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

// =====================================================================
//  OrderFlowTickExporter — places NO orders.
//  Run it in the Strategy Analyzer (1 Tick series, Tick Replay ON) over any
//  date range. It writes every trade with its aggressor side to:
//    Documents\NinjaTrader 8\orderflow_<instrument>_<from>_<to>.csv
//  in the format the Python backtester reads:
//    python run_backtest.py --data orderflow_MGC_12-26_....csv
//  This lets the Python research tools use NinjaTrader's own data feed —
//  no separate data subscription needed.
// =====================================================================
namespace NinjaTrader.NinjaScript.Strategies
{
    public class OrderFlowTickExporter : Strategy
    {
        private StreamWriter writer;
        private string path;
        private double lastPrice;
        private int lastSide = 1;
        private long rows, buys;
        private bool tickRuleForced;
        private static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Exports Tick Replay trades with aggressor side to CSV for the Python backtester.";
                Name = "OrderFlowTickExporter";
                Calculate = Calculate.OnEachTick;
                BarsRequiredToTrade = 0;
                UseTickRuleOnly = false;
            }
            else if (State == State.DataLoaded)
            {
                string inst = Instrument.FullName.Replace(" ", "_").Replace("/", "-");
                string range = DateTime.Now.ToString("yyyyMMdd_HHmmss");
                path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir,
                                    "orderflow_" + inst + "_" + range + ".csv");
                writer = new StreamWriter(path, false);
                writer.WriteLine("ts,price,size,side");
            }
            else if (State == State.Terminated)
            {
                if (writer != null)
                {
                    writer.Flush();
                    writer.Dispose();
                    writer = null;
                    Print(string.Format("[Exporter] wrote {0} trades to {1} ({2:P1} buy-aggressor)",
                        rows, path, rows > 0 ? (double)buys / rows : 0));
                }
            }
        }

        protected override void OnBarUpdate() { }

        protected override void OnMarketData(MarketDataEventArgs e)
        {
            if (writer == null || BarsInProgress != 0 || e.MarketDataType != MarketDataType.Last)
                return;

            int side;
            bool quoteOk = !UseTickRuleOnly && !tickRuleForced && e.Ask > 0 && e.Bid > 0;
            if (quoteOk && e.Price >= e.Ask) side = 1;
            else if (quoteOk && e.Price <= e.Bid) side = -1;
            else if (lastPrice > 0 && e.Price > lastPrice) side = 1;
            else if (lastPrice > 0 && e.Price < lastPrice) side = -1;
            else side = lastSide;
            lastPrice = e.Price;
            lastSide = side;

            rows++;
            if (side > 0) buys++;
            if (!tickRuleForced && !UseTickRuleOnly && rows == 3000 && buys < 60)
            {
                tickRuleForced = true;
                Print("[Exporter] WARNING: no historical bid/ask in this data - switching to tick rule for the rest of the file.");
            }

            DateTime utc;
            try
            {
                utc = TimeZoneInfo.ConvertTimeToUtc(DateTime.SpecifyKind(e.Time, DateTimeKind.Unspecified),
                                                    NinjaTrader.Core.Globals.GeneralOptions.TimeZoneInfo);
            }
            catch
            {
                utc = e.Time.ToUniversalTime();
            }
            long ns = (utc - Epoch).Ticks * 100;
            writer.WriteLine(string.Format(CultureInfo.InvariantCulture, "{0},{1},{2},{3}",
                ns, e.Price, e.Volume, side));
        }

        [NinjaScriptProperty]
        [Display(Name = "Aggressor by tick rule only", Order = 1, GroupName = "Export")]
        public bool UseTickRuleOnly { get; set; }
    }
}
