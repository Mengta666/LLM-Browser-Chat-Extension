/**
 * shared.js — 共享常量和工具函数
 * 在 background.js (via importScripts) 和 sidepanel.html (via <script>) 中使用
 */

const MAX_PROMPT_LENGTH = 8000;
const CUSTOM_API_BASE_URLS_KEY = 'customApiBaseUrls';

function isPrivateIpv4Host(host) {
  return /^127\./.test(host)
    || /^10\./.test(host)
    || /^192\.168\./.test(host)
    || /^0\.0\.0\.0$/.test(host)
    || /^172\.(1[6-9]|2\d|3[01])\./.test(host)
    || /^169\.254\./.test(host)
    || /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(host);
}

function isPrivateOrLocalHost(hostname) {
  const rawHost = String(hostname || '').trim().toLowerCase();
  if (!rawHost) return false;

  const host = rawHost.startsWith('[') && rawHost.endsWith(']')
    ? rawHost.slice(1, -1)
    : rawHost;

  if (!host) return false;
  if (host === 'localhost' || host === '::1' || host === '::') return true;
  if (isPrivateIpv4Host(host)) return true;
  if (/^(?:fc|fd)[0-9a-f:]*$/i.test(host)) return true;
  if (/^fe80:/i.test(host)) return true;

  const ipv4MappedMatch = host.match(/^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i);
  if (ipv4MappedMatch) {
    return isPrivateIpv4Host(ipv4MappedMatch[1]);
  }

  return false;
}

function normalizeApiBaseUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const allowLocalHttp = url.protocol === 'http:' && isPrivateOrLocalHost(url.hostname);
    if ((url.protocol !== 'https:' && !allowLocalHttp) || url.search || url.hash || url.username || url.password) return '';
    return `${url.origin}${url.pathname.replace(/\/$/, '')}`;
  } catch {
    return '';
  }
}
