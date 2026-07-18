import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "/Users/adnankhan/Downloads/pnl-BP2128 (3).xlsx";
const outputDir = "/Users/adnankhan/stock_research_automation/outputs/option_trader_charts";
const outputPath = `${outputDir}/pnl-BP2128-option-performance-dashboard.xlsx`;
const previewPath = `${outputDir}/dashboard-preview.png`;

const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);

const fo = workbook.worksheets.getItem("F&O");
const raw = fo.getRange("B38:N62").values;
const headers = raw[0].map((v) => String(v ?? "").trim());
const rows = raw.slice(1).filter((row) => row[0]);

const summaryRows = fo.getRange("B15:C18").values;
const summary = Object.fromEntries(summaryRows.map(([k, v]) => [String(k), Number(v || 0)]));
const charges = summary["Charges"] ?? 0;
const grossRealized = summary["Realized P&L"] ?? rows.reduce((sum, row) => sum + Number(row[5] || 0), 0);
const netAfterCharges = grossRealized - charges + (summary["Other Credit & Debit"] ?? 0);

function parseSymbol(symbol) {
  const type = symbol.slice(-2);
  const body = symbol.slice(0, -2);
  const strikeMatch = body.match(/(\d{5})$/);
  const strike = strikeMatch ? Number(strikeMatch[1]) : null;
  const prefix = strikeMatch ? body.slice(0, -5) : body;
  const underlyingMatch = prefix.match(/^([A-Z]+)/);
  const underlying = underlyingMatch ? underlyingMatch[1] : "";
  const expiryCode = prefix.slice(underlying.length);
  return { underlying, expiryCode, strike, type };
}

const trades = rows.map((row, i) => {
  const symbol = String(row[0]);
  const parsed = parseSymbol(symbol);
  const buy = Number(row[3] || 0);
  const sell = Number(row[4] || 0);
  const pnl = Number(row[5] || 0);
  return {
    tradeNo: i + 1,
    symbol,
    ...parsed,
    quantity: Number(row[2] || 0),
    buy,
    sell,
    pnl,
    pnlPct: Number(row[6] || 0) / 100,
    turnover: buy + sell,
    sourceRow: i + 39,
  };
});

let cumulative = 0;
let peak = 0;
for (const trade of trades) {
  cumulative += trade.pnl;
  peak = Math.max(peak, cumulative);
  trade.cumulative = cumulative;
  trade.drawdown = cumulative - peak;
}

const winners = trades.filter((trade) => trade.pnl > 0);
const losers = trades.filter((trade) => trade.pnl < 0);
const totalWins = winners.reduce((sum, trade) => sum + trade.pnl, 0);
const totalLosses = losers.reduce((sum, trade) => sum + trade.pnl, 0);
const avgWin = winners.length ? totalWins / winners.length : 0;
const avgLoss = losers.length ? totalLosses / losers.length : 0;
const profitFactor = totalLosses ? totalWins / Math.abs(totalLosses) : null;
const winRate = trades.length ? winners.length / trades.length : 0;
const maxDrawdown = Math.min(...trades.map((trade) => trade.drawdown), 0);

function groupBy(items, keyFn) {
  const map = new Map();
  for (const item of items) {
    const key = keyFn(item);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(item);
  }
  return map;
}

const typeRows = [...groupBy(trades, (trade) => trade.type).entries()]
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([type, items]) => {
    const pnl = items.reduce((sum, trade) => sum + trade.pnl, 0);
    const wins = items.filter((trade) => trade.pnl > 0).length;
    return [type, items.length, pnl, wins / items.length, pnl / items.length];
  });

const strikeRows = [...groupBy(trades, (trade) => String(trade.strike ?? "Unknown")).entries()]
  .map(([strike, items]) => {
    const ce = items.filter((trade) => trade.type === "CE").reduce((sum, trade) => sum + trade.pnl, 0);
    const pe = items.filter((trade) => trade.type === "PE").reduce((sum, trade) => sum + trade.pnl, 0);
    return [Number(strike), ce, pe, ce + pe];
  })
  .sort((a, b) => a[0] - b[0]);

const bins = [
  ["<= -1000", -Infinity, -1000],
  ["-1000 to -500", -1000, -500],
  ["-500 to 0", -500, 0],
  ["0 to 500", 0, 500],
  ["500 to 1000", 500, 1000],
  ["> 1000", 1000, Infinity],
].map(([label, min, max]) => [
  label,
  trades.filter((trade) => trade.pnl > min && trade.pnl <= max).length,
]);

const leaderboard = [...trades]
  .sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl))
  .slice(0, 12)
  .map((trade) => [trade.symbol, trade.type, trade.strike, trade.pnl, trade.pnlPct]);

const tradesSheet = workbook.worksheets.add("Trades Clean");
tradesSheet.showGridLines = false;
const tradeHeader = [
  "Trade #",
  "Symbol",
  "Underlying",
  "Expiry Code",
  "Strike",
  "Type",
  "Quantity",
  "Buy Value",
  "Sell Value",
  "Realized P&L",
  "Realized P&L %",
  "Turnover",
  "Result",
  "Cumulative P&L",
  "Drawdown",
  "Source Row",
];
tradesSheet.getRange("A1:P1").values = [tradeHeader];
tradesSheet.getRange(`A2:P${trades.length + 1}`).values = trades.map((trade) => [
  trade.tradeNo,
  trade.symbol,
  trade.underlying,
  trade.expiryCode,
  trade.strike,
  trade.type,
  trade.quantity,
  trade.buy,
  trade.sell,
  trade.pnl,
  trade.pnlPct,
  trade.turnover,
  trade.pnl > 0 ? "Win" : trade.pnl < 0 ? "Loss" : "Flat",
  trade.cumulative,
  trade.drawdown,
  trade.sourceRow,
]);
tradesSheet.getRange("A1:P1").format.fill = { type: "solid", color: "#1F2937" };
tradesSheet.getRange("A1:P1").format.font = { color: "#FFFFFF", bold: true };
tradesSheet.getRange(`A1:P${trades.length + 1}`).format.borders = { preset: "inside", style: "thin", color: "#E5E7EB" };
tradesSheet.getRange(`H2:J${trades.length + 1}`).setNumberFormat("#,##0.00");
tradesSheet.getRange(`K2:K${trades.length + 1}`).setNumberFormat("0.00%");
tradesSheet.getRange(`L2:O${trades.length + 1}`).setNumberFormat("#,##0.00");
tradesSheet.getRange("A:P").format.autofitColumns();
tradesSheet.freezePanes.freezeRows(1);

const dataSheet = workbook.worksheets.add("Chart Data");
dataSheet.showGridLines = false;
dataSheet.getRange("A1").values = [["Equity Curve"]];
dataSheet.getRange("A2:D2").values = [["Trade #", "Realized P&L", "Cumulative P&L", "Drawdown"]];
dataSheet.getRange(`A3:D${trades.length + 2}`).values = trades.map((trade) => [
  trade.tradeNo,
  trade.pnl,
  trade.cumulative,
  trade.drawdown,
]);
dataSheet.getRange("F1").values = [["Option Type Split"]];
dataSheet.getRange("F2:J2").values = [["Type", "Trades", "P&L", "Win Rate", "Avg P&L"]];
dataSheet.getRange(`F3:J${typeRows.length + 2}`).values = typeRows;
dataSheet.getRange("L1").values = [["Strike Wise P&L"]];
dataSheet.getRange("L2:O2").values = [["Strike", "CE P&L", "PE P&L", "Total P&L"]];
dataSheet.getRange(`L3:O${strikeRows.length + 2}`).values = strikeRows;
dataSheet.getRange("Q1").values = [["P&L Distribution"]];
dataSheet.getRange("Q2:R2").values = [["P&L Bucket", "Trades"]];
dataSheet.getRange(`Q3:R${bins.length + 2}`).values = bins;
dataSheet.getRange("T1").values = [["Largest P&L Swings"]];
dataSheet.getRange("T2:X2").values = [["Symbol", "Type", "Strike", "P&L", "Return %"]];
dataSheet.getRange(`T3:X${leaderboard.length + 2}`).values = leaderboard;
dataSheet.getRange("Z1").values = [["Charges Impact"]];
dataSheet.getRange("Z2:AA2").values = [["Metric", "Amount"]];
dataSheet.getRange("Z3:AA5").values = [
  ["Gross realized P&L", grossRealized],
  ["Charges", -charges],
  ["Net after charges", netAfterCharges],
];
for (const range of ["A1", "F1", "L1", "Q1", "T1", "Z1"]) {
  dataSheet.getRange(range).format.font = { bold: true, color: "#111827" };
}
for (const range of ["A2:D2", "F2:J2", "L2:O2", "Q2:R2", "T2:X2", "Z2:AA2"]) {
  dataSheet.getRange(range).format.fill = { type: "solid", color: "#E5E7EB" };
  dataSheet.getRange(range).format.font = { bold: true };
}
dataSheet.getRange("A:AA").format.autofitColumns();
dataSheet.getRange(`B3:D${trades.length + 2}`).setNumberFormat("#,##0.00");
dataSheet.getRange(`H3:H${typeRows.length + 2}`).setNumberFormat("#,##0.00");
dataSheet.getRange(`I3:I${typeRows.length + 2}`).setNumberFormat("0.0%");
dataSheet.getRange(`J3:J${typeRows.length + 2}`).setNumberFormat("#,##0.00");
dataSheet.getRange(`M3:O${strikeRows.length + 2}`).setNumberFormat("#,##0.00");
dataSheet.getRange(`W3:W${leaderboard.length + 2}`).setNumberFormat("#,##0.00");
dataSheet.getRange(`X3:X${leaderboard.length + 2}`).setNumberFormat("0.00%");
dataSheet.getRange("AA3:AA5").setNumberFormat("#,##0.00");
dataSheet.freezePanes.freezeRows(2);

const dashboard = workbook.worksheets.add("Option Dashboard");
dashboard.showGridLines = false;
dashboard.getRange("A1:R58").format.fill = { type: "solid", color: "#FFFFFF" };
dashboard.getRange("A1:R1").merge();
dashboard.getRange("A1").values = [["Option Trading Performance Dashboard"]];
dashboard.getRange("A2:R2").merge();
dashboard.getRange("A2").values = [["Client BP2128 | F&O P&L from 2026-06-01 to 2026-07-15 | Trade-level view from source workbook"]];
dashboard.getRange("A1:R2").format.fill = { type: "solid", color: "#111827" };
dashboard.getRange("A1").format.font = { color: "#FFFFFF", bold: true, fontSize: 18 };
dashboard.getRange("A2").format.font = { color: "#D1D5DB", fontSize: 10 };
dashboard.getRange("A1:R1").format.rowHeight = 28;
dashboard.getRange("A2:R2").format.rowHeight = 22;

const kpis = [
  ["Gross P&L", grossRealized, "#0F766E"],
  ["Charges", charges, "#B45309"],
  ["Net After Charges", netAfterCharges, "#B91C1C"],
  ["Win Rate", winRate, "#1D4ED8"],
  ["Profit Factor", profitFactor, "#4338CA"],
  ["Max Drawdown", maxDrawdown, "#BE123C"],
  ["Trades", trades.length, "#374151"],
  ["Winners", winners.length, "#15803D"],
  ["Losers", losers.length, "#B91C1C"],
  ["Avg Win", avgWin, "#15803D"],
  ["Avg Loss", avgLoss, "#B91C1C"],
  ["Largest Trade", Math.max(...trades.map((trade) => trade.pnl)), "#0F766E"],
];
for (let i = 0; i < kpis.length; i += 1) {
  const row = i < 6 ? 4 : 6;
  const col = (i % 6) * 3;
  const labelCell = dashboard.getCell(row - 1, col);
  const valueCell = dashboard.getCell(row, col);
  labelCell.values = [[kpis[i][0]]];
  valueCell.values = [[kpis[i][1]]];
  dashboard.getRangeByIndexes(row - 1, col, 2, 2).format.fill = { type: "solid", color: "#F9FAFB" };
  dashboard.getRangeByIndexes(row - 1, col, 2, 2).format.borders = { preset: "outside", style: "thin", color: "#D1D5DB" };
  labelCell.format.font = { color: "#374151", bold: true, fontSize: 9 };
  valueCell.format.font = { color: kpis[i][2], bold: true, fontSize: 12 };
}
dashboard.getRange("A5:P5").setNumberFormat("#,##0.00");
dashboard.getRange("J5").setNumberFormat("0.0%");
dashboard.getRange("A7:P7").setNumberFormat("#,##0.00");
dashboard.getRange("A7:G7").setNumberFormat("#,##0");

const tableStartRow = 61;
dashboard.getRange(`A${tableStartRow}:E${tableStartRow}`).values = [["Largest Swings", "Type", "Strike", "P&L", "Return %"]];
dashboard.getRange(`A${tableStartRow + 1}:E${leaderboard.length + tableStartRow}`).values = leaderboard;
dashboard.getRange(`G${tableStartRow}:J${tableStartRow}`).values = [["Option Type", "Trades", "P&L", "Win Rate"]];
dashboard.getRange(`G${tableStartRow + 1}:J${typeRows.length + tableStartRow}`).values = typeRows.map((row) => [row[0], row[1], row[2], row[3]]);
dashboard.getRange(`A${tableStartRow}:J${tableStartRow}`).format.fill = { type: "solid", color: "#1F2937" };
dashboard.getRange(`A${tableStartRow}:J${tableStartRow}`).format.font = { color: "#FFFFFF", bold: true };
dashboard.getRange(`D${tableStartRow + 1}:D${leaderboard.length + tableStartRow}`).setNumberFormat("#,##0.00");
dashboard.getRange(`E${tableStartRow + 1}:E${leaderboard.length + tableStartRow}`).setNumberFormat("0.00%");
dashboard.getRange(`I${tableStartRow + 1}:I${typeRows.length + tableStartRow}`).setNumberFormat("#,##0.00");
dashboard.getRange(`J${tableStartRow + 1}:J${typeRows.length + tableStartRow}`).setNumberFormat("0.0%");
dashboard.getRange("A:R").format.columnWidth = 13;
dashboard.getRange("A:A").format.columnWidth = 21;
dashboard.getRange("G:G").format.columnWidth = 14;

function addChart(type, config) {
  const chart = dashboard.charts.add(type, config);
  chart.chartFill = { type: "solid", color: "#FFFFFF" };
  chart.plotAreaFill = { type: "solid", color: "#FFFFFF" };
  return chart;
}

addChart("line", {
  title: "Equity Curve and Drawdown",
  categories: trades.map((trade) => trade.tradeNo),
  series: [
    { name: "Cumulative P&L", values: trades.map((trade) => trade.cumulative) },
    { name: "Drawdown", values: trades.map((trade) => trade.drawdown) },
  ],
  hasLegend: true,
  legend: { position: "bottom" },
  yAxis: { numberFormatCode: "#,##0" },
  from: { row: 8, col: 0 },
  extent: { widthPx: 600, heightPx: 300 },
});

addChart("bar", {
  title: "Largest P&L Swings by Symbol",
  categories: leaderboard.map((row, i) => `${i + 1}. ${row[2]}${row[1]}`),
  series: [{ name: "Realized P&L", values: leaderboard.map((row) => row[3]) }],
  hasLegend: false,
  barOptions: { direction: "bar", grouping: "clustered", gapWidth: 80 },
  yAxis: { numberFormatCode: "#,##0" },
  from: { row: 8, col: 9 },
  extent: { widthPx: 600, heightPx: 300 },
});

addChart("bar", {
  title: "Strike Wise P&L: CE vs PE",
  categories: strikeRows.map((row) => String(row[0])),
  series: [
    { name: "CE P&L", values: strikeRows.map((row) => row[1]) },
    { name: "PE P&L", values: strikeRows.map((row) => row[2]) },
  ],
  hasLegend: true,
  legend: { position: "bottom" },
  barOptions: { direction: "column", grouping: "clustered", gapWidth: 70 },
  yAxis: { numberFormatCode: "#,##0" },
  from: { row: 23, col: 0 },
  extent: { widthPx: 600, heightPx: 300 },
});

addChart("bar", {
  title: "Trade P&L Distribution",
  categories: bins.map((row) => row[0]),
  series: [{ name: "Trades", values: bins.map((row) => row[1]) }],
  hasLegend: false,
  barOptions: { direction: "column", grouping: "clustered", gapWidth: 60 },
  yAxis: { numberFormatCode: "0" },
  from: { row: 23, col: 9 },
  extent: { widthPx: 600, heightPx: 300 },
});

addChart("bar", {
  title: "Gross P&L vs Charges vs Net",
  categories: ["Gross P&L", "Charges", "Net After Charges"],
  series: [{ name: "Amount", values: [grossRealized, -charges, netAfterCharges] }],
  hasLegend: false,
  barOptions: { direction: "column", grouping: "clustered", gapWidth: 90 },
  yAxis: { numberFormatCode: "#,##0" },
  dataLabels: { showValue: true, position: "outEnd", textStyle: { fontSize: 9 } },
  from: { row: 39, col: 9 },
  extent: { widthPx: 600, heightPx: 250 },
});

dashboard.freezePanes.freezeRows(2);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
  maxChars: 4000,
});
console.log(errors.ndjson);

const check = await workbook.inspect({
  kind: "table,region,drawing",
  sheetId: "Option Dashboard",
  range: "A1:L45",
  maxChars: 6000,
  tableMaxRows: 12,
  tableMaxCols: 12,
});
console.log(check.ndjson);

await fs.mkdir(outputDir, { recursive: true });
const preview = await workbook.render({ sheetName: "Option Dashboard", range: "A1:R58", scale: 1, format: "png" });
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, previewPath }));
