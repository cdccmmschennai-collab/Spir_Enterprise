// Extraction template the user picks before uploading. Sent to the API as
// `extraction_mode`; the backend enforces it (Standard never switches to
// Saipem, Saipem rejects a workbook that is not a Saipem SPIR form).

export type ExtractionMode = "standard" | "saipem";

export interface ExtractionModeOption {
  value: ExtractionMode;
  label: string;
  description: string;
}

export const EXTRACTION_MODES: readonly ExtractionModeOption[] = [
  { value: "standard", label: "Standard", description: "Extract SPIR using the standard template." },
  { value: "saipem", label: "Saipem", description: "Extract SPIR using the Saipem template." },
];

export const DEFAULT_EXTRACTION_MODE: ExtractionMode = "standard";

export const extractionModeLabel = (mode: ExtractionMode) =>
  EXTRACTION_MODES.find((m) => m.value === mode)?.label ?? mode;

/** A stored / received value as a known mode, or null. */
export function parseExtractionMode(value: unknown): ExtractionMode | null {
  return EXTRACTION_MODES.some((m) => m.value === value) ? (value as ExtractionMode) : null;
}

/** Request fields (form data or JSON body) carrying the selected mode. */
export function extractionModeFields(mode: ExtractionMode): Record<string, string> {
  return { extraction_mode: mode };
}
