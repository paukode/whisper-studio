/** A Nova 2 Sonic voice as reported by GET /api/voice/status. */
export interface VoiceOption {
  id: string;
  label: string;
  locale: string;
  /** "yes" for Tiffany/Matthew, which speak every supported language. */
  polyglot: string;
}
