// Exact shape of the POST /extract response
export interface ExtractResponse {
  job_id:          string
  status:          'done' | 'queued' | 'processing' | 'failed'
  background:      boolean

  // Metadata
  format:          string | null
  spir_no:         string | null
  equipment:       string | null
  manufacturer:    string | null
  supplier:        string | null
  spir_type:       string | null
  eqpt_qty:        number | null
  spare_items:     number | null
  total_tags:      number | null
  annexure_count:  number | null
  annexure_stats:  Record<string, number> | null

  // Duplicate stats
  dup1_count:      number | null
  sap_count:       number | null
  total_rows:      number | null

  // Preview table — 27 columns, up to 12 rows
  preview_cols:    string[] | null
  preview_rows:    (string | null)[][] | null

  // Download
  file_id:         string | null
  filename:        string | null
}

// Login response
export interface TokenResponse {
  access_token:  string
  refresh_token: string
  token_type:    string
  expires_in:    number
}

// Upload state
export type UploadStatus = 'idle' | 'uploading' | 'success' | 'error'
