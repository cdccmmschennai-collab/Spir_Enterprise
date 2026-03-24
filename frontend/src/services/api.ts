import axios, { type AxiosProgressEvent } from 'axios'
import type { ExtractResponse, TokenResponse } from '../types/spir'

// ── Base client ───────────────────────────────────────────────────────────────
const BASE = 'http://127.0.0.1:8000'

const client = axios.create({ baseURL: BASE })

// Attach Bearer token from localStorage if present
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('spir_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// On 401 — clear token so LoginModal re-appears
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      localStorage.removeItem('spir_token')
      // Zustand store will react to this on next render
    }
    return Promise.reject(error)
  }
)

// ── Auth ──────────────────────────────────────────────────────────────────────
export async function login(username: string, password: string): Promise<TokenResponse> {
  const form = new URLSearchParams()
  form.append('username', username)
  form.append('password', password)
  const { data } = await client.post<TokenResponse>('/auth/login', form, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  })
  return data
}

// ── Extract ───────────────────────────────────────────────────────────────────
export async function extractSpir(
  file: File,
  onUploadProgress?: (pct: number) => void
): Promise<ExtractResponse> {
  const fd = new FormData()
  fd.append('file', file)

  const { data } = await client.post<ExtractResponse>('/extract', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: (e: AxiosProgressEvent) => {
      if (e.total && onUploadProgress) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100))
      }
    },
    timeout: 600_000, // 10 minutes for large files
  })
  return data
}

// ── Download ──────────────────────────────────────────────────────────────────
export function getDownloadUrl(fileId: string): string {
  const token = localStorage.getItem('spir_token')
  // Open in new tab — browser handles the download
  // Token sent via URL param as fallback since new tab can't set headers
  return `${BASE}/download/${fileId}${token ? `?token=${token}` : ''}`
}

export async function downloadFile(fileId: string, filename: string): Promise<void> {
  const token = localStorage.getItem('spir_token')
  const response = await client.get(`/download/${fileId}`, {
    responseType: 'blob',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })

  // Create a temporary anchor and trigger download
  const url  = window.URL.createObjectURL(new Blob([response.data]))
  const link = document.createElement('a')
  link.href  = url
  link.setAttribute('download', filename || 'SPIR_Extraction.xlsx')
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

// ── Health ────────────────────────────────────────────────────────────────────
export async function healthCheck(): Promise<boolean> {
  try {
    await client.get('/health', { timeout: 3000 })
    return true
  } catch {
    return false
  }
}
