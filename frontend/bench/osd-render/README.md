# OpenSeadragon rendering bench

Measures what a viewer *sees* under different OpenSeadragon settings — the
viewer-side counterpart of the admin Performance page, which measures the
server and the network. It answers questions such as "does
`immediate_render` / `max_image_cache_count` change anything on an iPad-sized
screen?" with numbers instead of impressions. `docs/developer-guide.md`
("Viewer rendering tuning") summarises the results it produced.

- `server.py` serves a synthetic 100,000 × 80,000 px Deep Zoom pyramid (256 px
  tiles, overlap 1 — Tessera's defaults) whose tiles are solid colours that
  encode their level, with an `ETag` and a configurable `Cache-Control`:
  `no-cache` by default (so an evicted tile costs a revalidation, 304), or
  what Tessera sends, `private, max-age=86400`, with the plan's `cc`. A fixed per-request latency stands in for the network and nginx. It
  logs every tile request (200 or 304).
- `harness.html` opens the app's OpenSeadragon build with the settings in its
  query string and, on every frame, reads the canvas back to classify each
  on-screen pixel of the image as *blank* (nothing drawn), *low-res* (a
  coarser level stretched) or *sharp* (the level OSD wants for that zoom).
- `run.mjs` drives it in headless Chromium through scripted scenarios:
  - `zoomin` — four ×2 zooms at the centre, back home, one ×16 jump;
  - `deep` — zoom ×32, pan 13 screens, zoom back home;
  - `panback` — zoom ×32, pan 6 screens out and the same 6 back,

  and records per step: time until the view settled (animation over and
  nothing loading, for 5 frames in a row; the time ends at the first of them),
  when the animation ended and when 95 % of the image was first sharp after it,
  tiles OSD loaded, tile requests that reached the server (200 / 304), the
  blank and low-res area integrated over time (ms × fraction of the image
  area), the largest blank fraction of any frame, frames drawn and OSD's cache
  occupancy.
- `analyze.mjs` turns result files into a Markdown table (medians over
  repetitions).

## Running

```sh
cd frontend
npm ci                                   # OpenSeadragon build the app ships
npm install --no-save playwright@1.56.1  # not an app dependency
npx playwright install chromium

# The tile server needs Pillow: the backend venv (`make venv` at the repo
# root) has it. Ubuntu's own python3 refuses a bare `pip install` (PEP 668).
../.venv/bin/python bench/osd-render/server.py 8765 &
node bench/osd-render/run.mjs out.json '[{"scenario":"deep","vp":"ipad","latency":30,"ir":1,"cache":200,"mtpf":4,"cc":"private, max-age=86400","reps":2}]'
node bench/osd-render/analyze.mjs out.json
```

The plan (inline JSON, as above, or a file name) is an array of runs, each
with `scenario`, `vp` (`fhd` 1920×1080 at pixel ratio 1, `hidpi` 1440×900 at
2, `ipad` 1180×820 at 2 and `phone` 390×844 at 3, the last two with touch),
`latency` (ms), `ir` (`immediateRender`, 0 / 1) and `cache`
(`maxImageCacheCount`), plus optional `mtpf`, `cc`, `drawer` (`canvas` by
default; the app itself keeps OpenSeadragon's default, which picks canvas on
iPad / iPhone and WebGL elsewhere), `sample` and `reps` (1). Results are
appended to the output file, so runs accumulate across invocations; set
`BENCH_BASE` if the server is not at `http://127.0.0.1:8765`.

A plan entry's defaults (`mtpf` 1, `cc` `no-cache`) are a baseline without
Tessera's tuning. To measure what ships, pass the viewers'
`maxTilesPerFrame` for that viewport's pixel ratio (`osdMaxTilesPerFrame` in
`src/lib/osd.ts`: 1 for `fhd`, 4 for `hidpi` / `ipad` / `phone`) and
`"cc": "private, max-age=86400"`.

## Reading the numbers

Headless Chromium rasterises in software, so frame rates are far lower than
on a real device — 15–25 fps at a pixel ratio of 2 here — and with them
everything OSD paces per frame: at `maxTilesPerFrame` 1 the load rate *is*
the frame rate, so a speed-up from a larger limit measured here overstates
what a 60 fps device gains. Read absolute times as specific to the measuring host,
not a device, and compare settings under the same viewport.

Request counts depend on timing too: tiles queued during a zoom animation
(levels it passes through) grow with the frames it lasts and the per-frame
limit, and Chromium reuses an image already loaded in the page without asking
the server even under `no-cache` — so compare `tiles loaded` as well as the
server's 200 / 304 counts, and repeat runs (`reps`). One screen's tile count
also varies up to 4× with the zoom (OSD draws the level whose pixel ratio is
between 0.5 and 1), so a per-screen figure holds for the zoom it was measured
at. Per-frame canvas sampling itself costs frame time; pass `"sample": 0` for
timing-only runs.
