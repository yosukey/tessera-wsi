// Deep Zoom URLs for a slide, versioned by its WSI content generation.
//
// The API reports each slide's `tile_version` (the server's
// `Slide.wsi_generation`, bumped by every WSI replace). Every /dzi/ URL carries
// it as `?v=N`: nginx keys its 30-day shared tile cache on the path plus that
// one argument, so after a replace the viewer asks for URLs no tile of the old
// scan was ever stored under — instead of being served a mix of cached old
// tiles and freshly rendered new ones from the unchanged path. The server 404s
// any `v` that is not the slide's current generation, so a stale tab fails
// cleanly rather than mixing generations.
//
// Only the descriptor / archive URL needs it explicitly: OpenSeadragon's
// DziTileSource copies the `.dzi` URL's query string onto every tile URL it
// derives (`_files/{level}/{col}_{row}.jpeg?v=N`), and szi-tile-source sends
// every Range read to the archive URL as given.
//
// Kept free of OpenSeadragon so non-viewer code (the admin browser perf test)
// can build the same URLs without pulling the viewer into its chunk.

/**
 * `?v=N` for a known generation, '' otherwise. A missing version (a response
 * from an endpoint that doesn't report one) yields the
 * unversioned URL, which the server still answers — it is just never stored in
 * the shared edge cache. Only a canonical non-negative integer is emitted: the
 * server refuses any other spelling.
 */
export function tileVersionQuery(tileVersion: number | null | undefined): string {
  return typeof tileVersion === 'number' && Number.isSafeInteger(tileVersion) && tileVersion >= 0
    ? `?v=${tileVersion}`
    : ''
}

/** The OpenSlide-backed Deep Zoom descriptor: `/dzi/{public_id}.dzi?v=N`. */
export function dziDescriptorUrl(slidePublicId: string, tileVersion?: number | null): string {
  return `/dzi/${encodeURIComponent(slidePublicId)}.dzi${tileVersionQuery(tileVersion)}`
}

/** The raw SZI archive szi-tile-source reads by Range: `/dzi/{public_id}.szi?v=N`. */
export function sziArchiveUrl(slidePublicId: string, tileVersion?: number | null): string {
  return `/dzi/${encodeURIComponent(slidePublicId)}.szi${tileVersionQuery(tileVersion)}`
}

/**
 * One tile, spelled exactly as OpenSeadragon derives it from
 * `dziDescriptorUrl` — for code that fetches tiles itself and must hit the
 * same cache entries a viewer populates.
 */
export function dziTileUrl(
  slidePublicId: string,
  level: number,
  col: number,
  row: number,
  format: string,
  tileVersion?: number | null,
): string {
  return `/dzi/${encodeURIComponent(slidePublicId)}_files/${level}/${col}_${row}.${format}${tileVersionQuery(tileVersion)}`
}
