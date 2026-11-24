import { describe, expect, it } from 'vitest'
import { csvCell, csvSafe } from './csvSafe'

// The cases of services/app/tests/test_csv_export.py, so the frontend's CSVs
// are defanged exactly like the server's exports.

describe('csvSafe', () => {
  it.each([
    ['=1+1', "'=1+1"],
    ['+1', "'+1"],
    ['-1', "'-1"],
    ['@SUM(A1)', "'@SUM(A1)"],
    ['\t=1', "'\t=1"],
    ['\r=1', "'\r=1"],
    // A leading LF, and whitespace a trimming import would drop first.
    ['\n=1', "'\n=1"],
    [' =HYPERLINK("http://x")', "' =HYPERLINK(\"http://x\")"],
    ['  \n @x', "'  \n @x"],
    // A ';'-separated part is a cell of its own in a ';'-separator locale.
    ['Mozilla/5.0;=HYPERLINK(http://x)', "Mozilla/5.0;'=HYPERLINK(http://x)"],
    ['a;=1+1', "a;'=1+1"],
    ['/a; =1;b;+2', "/a;' =1;b;'+2"],
    ['=a;=b', "'=a;'=b"],
    ['a;\t=1', "a;'\t=1"],
    // Quoting does not keep a value whole in a ';' locale (the commas between
    // columns are not separators there), so its parts count too.
    ['=a,b;=c', "'=a,b;'=c"],
    ['x,y;=1+2;', "x,y;'=1+2;"],
    ['x"y;=1+5;', "x\"y;'=1+5;"],
    ['line\n;-1', "line\n;'-1"],
    // Crawler User-Agents get a cosmetic quote: their " +http" part is a
    // formula once a trimming import drops the space.
    [
      'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
      "Mozilla/5.0 (compatible; Googlebot/2.1;' +http://www.google.com/bot.html)",
    ],
  ])('quotes formula-like parts of %j', (value, expected) => {
    expect(csvSafe(value)).toBe(expected)
  })

  it.each([
    '', 'plain', '10.0.0.99', 'a;b;c', 'Mozilla/5.0 (X11; Linux x86_64)',
    '  leading space', 'trailing =', 'a=b', ';', 'x;', 'say "hi"',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'x,y;z', 'say "hi";ok',
  ])('leaves %j unchanged', (value) => {
    expect(csvSafe(value)).toBe(value)
  })

  it.each([
    '=1', ' =1', 'a;=b;-c', '\n@x', 'plain', '=a;=b', ' =a;+b', '=a,b;=c',
    'x,y;=1+2;', 'a; +b',
  ])('is idempotent on %j', (value) => {
    const once = csvSafe(value)
    expect(csvSafe(once)).toBe(once)
  })
})

describe('csvCell', () => {
  it('defangs before quoting, like the server writer', () => {
    const row = ["UA;=cmd|'/c calc'!A0", ' =1'].map(csvCell).join(',')
    expect(row).toBe("UA;'=cmd|'/c calc'!A0,' =1")
    expect(csvCell('=a,b')).toBe('"\'=a,b"')
    expect(csvCell('say "hi"')).toBe('"say ""hi"""')
    expect(csvCell('line\n;-1')).toBe('"line\n;\'-1"')
  })

  // Read the file the way a ';'-separator spreadsheet does: split each line
  // on ';' only (commas and double quotes are plain characters there), and
  // require that no resulting cell starts with a formula character, even after
  // the leading whitespace a trimming import drops.
  it('leaves no ;-separated cell starting with a formula character', () => {
    const hostile = [
      'x,y;=1+2;', 'x"y;=1+5;', 'a,b;=HYPERLINK(CHAR(104),3);',
      "UA;=cmd|'/c calc'!A0", ' =1', 'p; +2', 'q;\t-3', 'line\n;@4',
    ]
    const body = hostile.map((v) => ['lead', v, 'tail'].map(csvCell).join(',')).join('\r\n')
    for (const line of body.split('\r\n')) {
      for (const cell of line.split(';')) {
        expect(cell.replace(/"/g, ''), line).not.toMatch(/^[\s"]*[=+\-@]/)
      }
    }
  })
})
