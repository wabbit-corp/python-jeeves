#!/usr/bin/env bash
set -euo pipefail

VOX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOX_PYTHON="$VOX_ROOT/.venv/bin/python"
browser_python=("$VOX_PYTHON")

if [[ "$(uname -s)" == Linux && "$EUID" == 0 ]]; then
  "$VOX_PYTHON" -m playwright install-deps chromium
  VOX_SERVICE_USER="${VOX_SERVICE_USER:-python-jeeves}"
  VOX_CACHE="${XDG_CACHE_HOME:-/var/cache/python-jeeves}"
  install -d -m 0750 -o "$VOX_SERVICE_USER" -g "$VOX_SERVICE_USER" "$VOX_CACHE"
  browser_python=(runuser -u "$VOX_SERVICE_USER" -- env
    HOME=/var/lib/python-jeeves XDG_CACHE_HOME="$VOX_CACHE" "$VOX_PYTHON")
fi

"${browser_python[@]}" -m playwright install chromium
"${browser_python[@]}" - <<'PY'
import asyncio
from playwright.async_api import async_playwright

async def check_browser():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<title>Vox browser check</title>')
            assert await page.title() == 'Vox browser check'
        finally:
            await browser.close()

asyncio.run(check_browser())
print('Chromium launch verified for the runtime account.')
PY
