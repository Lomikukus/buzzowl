/**
 * secrets.ts — decrypt per-org LLM keys stored by the Python side (plans.py).
 * Format: "enc:v1:" + base64url(nonce[12] | ciphertext | gcm-tag[16]),
 * AES-256-GCM, key = SHA-256(BUZZOWL_SECRET_KEY || agent service token || dev fallback).
 * Both processes derive the same key from the same env, so no key ever travels over HTTP.
 *
 * Because of that fallback, an install that never set BUZZOWL_SECRET_KEY encrypts with
 * the agent service token — rotating it makes every stored org key unreadable. Ciphertext
 * that no longer opens throws SecretDecryptError; callers that can carry on without the
 * secret use tryDecryptSecret() and treat it as "no key stored".
 */
import { createDecipheriv, createHash } from 'node:crypto';
import { config } from './config.js';

const PREFIX = 'enc:v1:';

export class SecretDecryptError extends Error {
  constructor(cause?: unknown) {
    super('stored secret cannot be decrypted — the encryption key changed '
      + '(set BUZZOWL_SECRET_KEY explicitly; rotating AGENT_SERVICE_TOKEN without it '
      + 'orphans every stored key)');
    this.name = 'SecretDecryptError';
    if (cause !== undefined) (this as { cause?: unknown }).cause = cause;
  }
}

function keyMaterial(): Buffer {
  const raw = process.env.BUZZOWL_SECRET_KEY || config.serviceToken || 'buzzowl-insecure-dev-key';
  return createHash('sha256').update(raw).digest();
}

export function isEncrypted(v: string | undefined | null): boolean {
  return !!v && v.startsWith(PREFIX);
}

/** Throws SecretDecryptError when the ciphertext does not open with the current key. */
export function decryptSecret(token: string | undefined | null): string {
  if (!token || !token.startsWith(PREFIX)) return token ?? '';
  try {
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
  } catch (err) {
    throw new SecretDecryptError(err);
  }
}

/** decryptSecret() that returns null instead of throwing, with one warning. */
export function tryDecryptSecret(token: string | undefined | null, what = 'a stored secret'): string | null {
  try {
    return decryptSecret(token);
  } catch (err) {
    if (!(err instanceof SecretDecryptError)) throw err;
    console.warn(`[secrets] ${what} cannot be decrypted — encryption key changed? `
      + 'Reconnect the key in Settings');
    return null;
  }
}
