// Mirror of services/app/wsi_app/csv_export.py:csv_safe, the spreadsheet
// formula-injection defang the server's admin CSV exports run every cell
// through, for the CSVs the frontend builds itself (the live-sync follower
// list, whose name / user-agent / hostname columns are attacker-controlled).
// Keep the two in lockstep; csvSafe.test.ts mirrors tests/test_csv_export.py.
//
// - A value that starts with =, +, -, @, TAB, CR or LF — also after leading
//   whitespace, which a trimming import drops before deciding — gets a leading
//   single quote so the cell is read as text.
// - Every ';'-separated part is checked the same way, whatever the CSV
//   quoting: a spreadsheet in a ';'-separator locale splits the text on ';',
//   and there the commas between columns are not separators, so a quoted
//   field's ';' still starts a new cell. The same goes for the parts after a
//   CR, LF or CRLF: such an import ends the row at a line break even inside
//   a quoted field, so the next part opens a new row's first cell.
// - Idempotent (a defanged part starts with the quote); a value with no such
//   part comes out unchanged.

const FORMULA_LEAD = ['=', '+', '-', '@', '\t', '\r', '\n']
// The whitespace a spreadsheet may trim before the formula check (" \t\r\n").
const LEADING_SPACE = /^[ \t\r\n]+/

function startsWithFormulaLead(s: string): boolean {
  return FORMULA_LEAD.some((c) => s.startsWith(c))
}

function formulaLike(part: string): boolean {
  return startsWithFormulaLead(part) || startsWithFormulaLead(part.replace(LEADING_SPACE, ''))
}

// Cell (';') and row (CR, LF, CRLF — CRLF first) boundaries of a
// ';'-separator import; captured so split() keeps them for the re-join.
const PART_SEP = /(;|\r\n|\r|\n)/

export function csvSafe(v: string): string {
  const head = formulaLike(v) ? `'${v}` : v
  const pieces = head.split(PART_SEP)
  if (pieces.length === 1) return head
  // pieces alternates part, separator, part, …; the first part is the start
  // of the value, already handled above.
  for (let i = 2; i < pieces.length; i += 2) {
    if (formulaLike(pieces[i])) pieces[i] = `'${pieces[i]}`
  }
  return pieces.join('')
}

// One CSV field: defanged, then quoted as Python's csv.writer (QUOTE_MINIMAL,
// "\r\n" line terminator) quotes it — only when it holds a comma, a double
// quote, CR or LF, with embedded double quotes doubled.
export function csvCell(v: string): string {
  const out = csvSafe(v)
  return /[,"\r\n]/.test(out) ? `"${out.replace(/"/g, '""')}"` : out
}
