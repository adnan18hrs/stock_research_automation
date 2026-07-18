import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "/Users/adnankhan/Downloads/pnl-BP2128 (3).xlsx";
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);

const summary = await workbook.inspect({
  kind: "workbook,sheet,table,region",
  maxChars: 12000,
  tableMaxRows: 12,
  tableMaxCols: 18,
  tableMaxCellChars: 80,
});

console.log(summary.ndjson);
