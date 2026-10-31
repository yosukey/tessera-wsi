// Ordering for attribute values shown as filter chips.
//
// Shared by the public index header and the admin dashboard's filter panel so
// the two screens list the same values in the same order. A bare
// Array.prototype.sort() compares UTF-16 code points, which puts "11" before
// "2" — a numbered category (case numbers, block ids, dilution steps) then
// reads as 1, 11, 12, 2, 3.
//
// `numeric: true` compares digit runs as numbers, and `sensitivity: 'base'`
// keeps case and accent variants of the same word adjacent instead of
// splitting them into separate uppercase/lowercase blocks. Values that differ
// only in case therefore compare equal; sort() is stable, so those keep the
// order the caller supplied.
export function compareValues(a: string, b: string): number {
  return a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true })
}
