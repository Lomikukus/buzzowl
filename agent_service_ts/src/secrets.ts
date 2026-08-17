/**
 * secrets.ts — decrypt per-org LLM keys stored by the Python side (plans.py).
 * Format: "enc:v1:" + base64url(nonce[12] | ciphertext | gcm-tag[16]),
 * AES-256-GCM, key = SHA-256(BUZZOWL_SECRET_KEY || agent service token || dev fallback).
 * Both processes derive the same key from the same env, so no key ever travels over HTTP.
 */
import { createDecipheriv, createHash } from 'node:crypto';
import { config } from './config.js';

const PREFIX = 'enc:v1:';

function keyMaterial(): Buffer {
  const raw = process.env.BUZZOWL_SECRET_KEY || config.serviceToken || 'buzzowl-insecure-dev-key';
  return createHash('sha256').update(raw).digest();
}

export function isEncrypted(v: string | undefined | null): boolean {
  return !!v && v.startsWith(PREFIX);
}

export function decryptSecret(token: string | undefined | null): string {
  if (!token || !token.startsWith(PREFIX)) return token ?? '';
  let b64 = token.slice(PREFIX.length).replace(/-/g, '+').replace(/_/g, '/');
  while (b64.length % 4) b64 += '=';
  const raw = Buffer.from(b64, 'base64');
  const nonce = raw.subarray(0, 12);
  const body = raw.subarray(12);
  const tag = body.subarray(body.length - 16);
  const ct = body.subarray(0, body.length - 16);
  const d = createDecipheriv('aes-256-gcm', keyMaterial(), nonce);
  d.setAuthTag(tag);
  return Buffer.concat([d.update(ct), d.final()]).toString('utf8');
}
