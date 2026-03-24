import { create } from 'zustand'
import type { ExtractResponse, UploadStatus } from '../types/spir'

interface SpirStore {
  // Auth
  token:     string | null
  setToken:  (t: string | null) => void

  // Upload
  status:    UploadStatus
  progress:  number               // 0-100
  filename:  string
  setStatus:   (s: UploadStatus) => void
  setProgress: (p: number) => void
  setFilename: (n: string) => void

  // Result
  result:    ExtractResponse | null
  error:     string | null
  setResult: (r: ExtractResponse | null) => void
  setError:  (e: string | null) => void

  // Reset
  reset: () => void
}

export const useStore = create<SpirStore>((set) => ({
  token:    localStorage.getItem('spir_token'),
  setToken: (t) => {
    if (t) localStorage.setItem('spir_token', t)
    else   localStorage.removeItem('spir_token')
    set({ token: t })
  },

  status:      'idle',
  progress:    0,
  filename:    '',
  setStatus:   (status)   => set({ status }),
  setProgress: (progress) => set({ progress }),
  setFilename: (filename) => set({ filename }),

  result:    null,
  error:     null,
  setResult: (result) => set({ result, error: null }),
  setError:  (error)  => set({ error, result: null }),

  reset: () => set({
    status:   'idle',
    progress: 0,
    filename: '',
    result:   null,
    error:    null,
  }),
}))
