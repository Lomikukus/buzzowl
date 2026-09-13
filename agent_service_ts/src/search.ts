import { config } from './config.js';

export interface SearchResult {
  url: string;
  title: string;
  snippet: string;
  publishedDate?: string;
}

export interface SearchOptions {
  category?: 'general' | 'news';
  timeRange?: 'day' | 'week' | 'month' | 'year';
  language?: string;
}

// -- SearXNG (primary, Docker-internal at http://searxng:8080) --

async function searxngSearch(query: string, n: number, opts: SearchOptions = {}): Promise<SearchResult[]> {
  const params = new URLSearchParams({
    q: query,
    format: 'json',
    language: opts.language ?? 'en',
    categories: opts.category ?? 'general',
  });
  if (opts.timeRange) params.set('time_range', opts.timeRange);
  const url = `${config.searxngUrl}/search?${params.toString()}`;
  const resp = await fetch(url, {
    headers: { 'Accept': 'application/json' },
    signal: AbortSignal.timeout(15_000),
  });
  if (!resp.ok) throw new Error(`SearXNG ${resp.status}`);
  const data = await resp.json() as { results?: { url: string; title: string; content: string; publishedDate?: string }[] };
  return (data.results ?? []).slice(0, n).map(r => ({
    url: r.url,
    title: r.title,
    snippet: r.content ?? '',
    ...(r.publishedDate ? { publishedDate: r.publishedDate } : {}),
  }));
}

// -- DuckDuckGo HTML fallback --

async function ddgSearch(query: string, n: number): Promise<SearchResult[]> {
  const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}&kl=en-us`;
  const resp = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0 WhisperKnowledge-Agent/1.0' },
    signal: AbortSignal.timeout(20_000),
  });
  if (!resp.ok) throw new Error(`DDG ${resp.status}`);
  const html = await resp.text();

  const results: SearchResult[] = [];
  // Extract: <a class="result__a" href="...">title</a>
  const linkRe = /class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</g;
  const snippetRe = /class="result__snippet"[^>]*>([^<]+)</g;
  let m: RegExpExecArray | null;
  let s: RegExpExecArray | null;

  while ((m = linkRe.exec(html)) !== null && results.length < n) {
    s = snippetRe.exec(html);
    const rawUrl = m[1];
    // DDG wraps in a redirect — extract ud= param or use as-is
    let finalUrl = rawUrl;
    try {
      const u = new URL(rawUrl.startsWith('http') ? rawUrl : `https://duckduckgo.com${rawUrl}`);
      finalUrl = u.searchParams.get('uddg') ?? u.searchParams.get('u') ?? rawUrl;
    } catch { /* keep rawUrl */ }
    results.push({ url: finalUrl, title: m[2].trim(), snippet: s ? s[1].trim() : '' });
  }
  return results;
}

export async function webSearch(query: string, nResults = 5, opts: SearchOptions = {}): Promise<SearchResult[]> {
  try {
    const res = await searxngSearch(query, nResults, opts);
    if (res.length > 0) return res;
  } catch { /* fall through */ }
  try {
    return await ddgSearch(query, nResults);  // opts (news category/time_range) have no DDG equivalent
  } catch {
    return [];
  }
}

// -- Page fetch (plain) --

export async function fetchPage(url: string, maxChars = 3000): Promise<string> {
  try {
    const resp = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0 WhisperKnowledge-Agent/1.0' },
      signal: AbortSignal.timeout(20_000),
    });
    if (!resp.ok) return `Error: HTTP ${resp.status}`;
    const ct = resp.headers.get('content-type') ?? '';
    if (ct.includes('pdf') || ct.includes('octet-stream') || ct.includes('zip')) {
      return `(skipped binary content: ${ct})`;
    }
    const html = await resp.text();
    const text = html
      .replace(/\0/g, '')
      .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
      .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/\s{2,}/g, ' ')
      .trim()
      .slice(0, maxChars);
    return text || '(no readable content)';
  } catch (err) {
    return `Error fetching page: ${String(err)}`;
  }
}

// -- Page fetch (browser-rendered via self-hosted Playwright service) --

export async function fetchPageBrowser(url: string, maxChars = 5000, waitMs = 3500): Promise<string> {
  try {
    const resp = await fetch(`${config.browserServiceUrl}/fetch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, max_chars: maxChars, wait_ms: waitMs }),
      signal: AbortSignal.timeout(45_000),
    });
    if (!resp.ok) throw new Error(`browser-service ${resp.status}`);
    const data = await resp.json() as { text?: string };
    return data.text || '(no readable content)';
  } catch {
    return fetchPage(url, maxChars);
  }
}

// -- Page fetch (Camofox: Firefox + fingerprint spoofing, JS-rendered, anti-bot) --
// Mirrors agent_service_hermes/search.py:fetch_page_camofox. Falls back to the
// browser service, then plain fetch, if Camofox is unavailable.

export async function fetchPageCamofox(url: string, maxChars = 8000): Promise<string> {
  if (!config.camofoxUrl) return fetchPageBrowser(url, maxChars);

  const userId = 'pi';
  let tabId: string | undefined;
  try {
    const createResp = await fetch(`${config.camofoxUrl}/tabs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, sessionKey: crypto.randomUUID(), url }),
      signal: AbortSignal.timeout(20_000),
    });
    if (!createResp.ok) throw new Error(`camofox ${createResp.status}`);
    const body = await createResp.json() as { tabId?: string; id?: string };
    tabId = body.tabId ?? body.id;
    if (!tabId) throw new Error('no tab id in Camofox response');

    // Give Camofox time to render before snapshotting (matches Hermes's 2s wait).
    await new Promise(r => setTimeout(r, 2000));

    const snap = await fetch(`${config.camofoxUrl}/tabs/${tabId}/snapshot?userId=${userId}`, {
      signal: AbortSignal.timeout(20_000),
    });
    if (!snap.ok) throw new Error(`camofox snapshot ${snap.status}`);
    const snapBody = await snap.json() as { snapshot?: string };
    const text = (snapBody.snapshot ?? '')
      .replace(/\n{3,}/g, '\n\n')
      .replace(/\0/g, '')
      .trim()
      .slice(0, maxChars);
    return text || '(no readable content)';
  } catch {
    return fetchPageBrowser(url, maxChars);
  } finally {
    if (tabId) {
      try {
        await fetch(`${config.camofoxUrl}/tabs/${tabId}?userId=${userId}`, {
          method: 'DELETE',
          signal: AbortSignal.timeout(10_000),
        });
      } catch { /* ignore cleanup failure */ }
    }
  }
}
