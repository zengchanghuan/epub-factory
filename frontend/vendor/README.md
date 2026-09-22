# Local QR renderer

`qrcode.js` bundles the browser entry of MIT-licensed `qrcode@1.5.4`
([upstream](https://github.com/soldair/node-qrcode)), including `dijkstrajs@1.0.3`.
The licenses are retained in `qrcode.LICENSE.txt`.

The previously referenced CDN build URL returned HTTP 404. Checkout now uses this
local asset on both the main and repair pages; no payment URL is sent to an
external QR image service.

To rebuild in a temporary directory with Node.js and pnpm available:

```sh
mkdir -p /tmp/epub-qr-vendor
pnpm --dir /tmp/epub-qr-vendor add qrcode@1.5.4 esbuild@0.25.10 --ignore-scripts
node /tmp/epub-qr-vendor/node_modules/esbuild/bin/esbuild \
  /tmp/epub-qr-vendor/node_modules/qrcode/lib/browser.js \
  --bundle --minify --format=iife --global-name=QRCode --platform=browser \
  --target=es2018 --outfile=frontend/vendor/qrcode.js \
  '--banner:js=/*! node-qrcode 1.5.4 | MIT | see qrcode.LICENSE.txt */'
```

Run from the repository root. Review dependency changes and preserve both licenses
when updating the bundle.
