/** Normalize Quartz's host[/base-path] configuration.
 *
 * Quartz prefixes `https://` when it emits absolute URLs, so storing a full
 * URL produces `https://https://...`. Accept either operator-friendly form
 * and keep the protocol-free value Quartz expects internally.
 */
export function normalizeBaseUrl(value: string): string {
  const normalized = value
    .trim()
    .replace(/^https?:\/\//i, "")
    .replace(/^\/+/, "")
    .replace(/\/+$/, "")

  if (!normalized) throw new Error("NLAB_SITE_URL must contain a hostname")
  const parsed = new URL(`https://${normalized}`)
  if (!parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error(`Invalid NLAB_SITE_URL: ${value}`)
  }
  return normalized
}
