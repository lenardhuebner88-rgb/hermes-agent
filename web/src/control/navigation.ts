export function legacyControlRedirectTarget(targetPath: string, search: string): string {
  // Abriss S6: legacy deep-links must preserve drawer/filter intent
  // (/control/flow?task=... → /control/fleet?task=..., etc.).
  return `${targetPath}${search}`;
}
