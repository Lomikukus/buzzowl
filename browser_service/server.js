'use strict';
const { chromium } = require('playwright');
const express = require('express');

const app = express();
app.use(express.json({ limit: '1mb' }));

const PORT = process.env.PORT || 3000;

let browser = null;

async function ensureBrowser() {
  if (!browser || !browser.isConnected()) {
    browser = await chromium.launch({
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
      headless: true,
    });
  }
  return browser;
}

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', service: 'browser-service' });
});

app.post('/fetch', async (req, res) => {
  const { url, max_chars = 8000, wait_ms = 1500 } = req.body ?? {};
  if (!url || typeof url !== 'string') {
    return res.status(400).json({ error: 'url (string) required' });
  }

  let page;
  try {
    const b = await ensureBrowser();
    page = await b.newPage();

    await page.setExtraHTTPHeaders({
      'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36',
    });

    // Skip images/fonts to speed up page load
    await page.route('**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot}', route => route.abort());

    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    if (wait_ms > 0) await page.waitForTimeout(wait_ms);

    const text = await page.evaluate(() => {
      document.querySelectorAll('script,style,nav,footer,header,[role="navigation"],[role="banner"]').forEach(el => el.remove());
      return (document.body?.innerText ?? '').replace(/\0/g, '');
    });

    const clean = text
      .replace(/[ \t]{2,}/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim()
      .slice(0, max_chars);

    res.json({ text: clean || '(no readable content)', url, chars: clean.length });
  } catch (err) {
    res.status(500).json({ error: String(err), url });
  } finally {
    if (page) await page.close().catch(() => {});
  }
});

process.on('SIGTERM', async () => {
  if (browser) await browser.close().catch(() => {});
  process.exit(0);
});

app.listen(PORT, () => console.log(`[browser-service] :${PORT}`));
