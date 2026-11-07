# Murmuratio landing page

Deploy `index.html`, `assets/`, and `logo/` as static files. Paths are relative, so the page works at a domain root or under a subdirectory. The font licenses in those directories accompany the included fonts, handwriting, and favicon. `logo/wordmark.svg` is the standalone version of the page's dot wordmark. Enable gzip or Brotli for HTML on the static host; the Lighthouse checks use gzip.

Development only; these scripts are not required by the deployed page:

- `serve.py` runs a loopback preview with gzip compression: `python3 site/serve.py --port 4817`.
- `build-fonts.py` rebuilds the self-hosted Geist subsets from an unpacked Geist package; see its header for dependencies.
- `build-annotations.py` rebuilds the hidden handwriting from a Caveat font; see its header for dependencies.

The handwritten guide is retained but hidden with `data-annotations="off"` in `index.html`. The dormant `startFlock` implementation and lede marker styles are retained there as well.
