// OpenSeadragon probes canvas support at import; jsdom has no canvas backend
// and logs "Not implemented: getContext" for every file that loads it. Report
// "no 2D context" instead, which OSD handles, so test output stays readable.
if (typeof HTMLCanvasElement !== 'undefined') {
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext
}
