import React, { memo, useCallback, useState } from 'react'
import { useDropzone } from 'react-dropzone'
import { Upload, FileSpreadsheet, X, AlertCircle } from 'lucide-react'
import { extractSpir } from '../services/api'
import { useStore } from '../store/useStore'
import { FileRejection } from 'react-dropzone'

const ACCEPTED = {
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
  'application/vnd.ms-excel.sheet.macroEnabled.12': ['.xlsm'],
  'application/vnd.ms-excel': ['.xls'],
}

function formatBytes(bytes: number): string {
  if (bytes < 1024)      return `${bytes} B`
  if (bytes < 1_048_576) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1_048_576).toFixed(2)} MB`
}

export const UploadBox = memo(() => {
  const { status, progress, setStatus, setProgress, setResult, setError, reset, setFilename } = useStore()
  const [file, setFile] = useState<File | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)

  const isIdle      = status === 'idle'
  const isUploading = status === 'uploading'

  const runExtraction = useCallback(async (f: File) => {
    setLocalError(null)
    setStatus('uploading')
    setProgress(0)

    // Simulate granular progress while server processes
    let fake = 0
    const ticker = setInterval(() => {
      fake = Math.min(fake + Math.random() * 4, 88)
      setProgress(Math.round(fake))
    }, 250)

    try {
      const result = await extractSpir(f, (pct) => {
        // Real upload progress — first 30%
        setProgress(Math.round(pct * 0.3))
      })
      clearInterval(ticker)
      setProgress(100)
      setTimeout(() => {
        setResult(result)
        setStatus('success')
      }, 400)
    } catch (err: unknown) {
      clearInterval(ticker)
      setStatus('error')
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string; error?: string } } }
        const detail   = axiosErr.response?.data?.detail
        const error    = axiosErr.response?.data?.error
        if (typeof detail === 'string') setLocalError(detail)
        else if (typeof error === 'string') setLocalError(error)
        else setLocalError('Extraction failed. Check your file and try again.')
      } else {
        setLocalError('Cannot reach the server. Is the backend running on port 8000?')
      }
    }
  }, [setStatus, setProgress, setResult, setError])

  

const onDrop = useCallback(
  (acceptedFiles: File[], fileRejections: FileRejection[]) => {
    if (fileRejections.length > 0) {
      setLocalError('Unsupported file. Upload .xlsx or .xlsm only.')
      return
    }

    if (acceptedFiles.length > 0) {
      const selectedFile = acceptedFiles[0]
      setFile(selectedFile)
      setFilename(selectedFile.name)
      setLocalError(null)
    }
  },
  [setFilename]
)

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept:   ACCEPTED,
    maxFiles: 1,
    disabled: isUploading,
  })

  const handleExtract = () => {
    if (file) runExtraction(file)
  }

  const handleReset = () => {
    setFile(null)
    setLocalError(null)
    reset()
  }

  return (
    <div className="w-full">
      {/* Drop zone */}
      <div
        {...getRootProps()}
        className={`
          relative border-2 border-dashed rounded-2xl p-10 text-center
          cursor-pointer transition-all duration-200 select-none
          ${isDragActive
            ? 'border-brand-500 bg-brand-50 scale-[1.01]'
            : file
              ? 'border-brand-400 bg-brand-50/60'
              : 'border-gray-200 bg-gray-50/50 hover:border-brand-300 hover:bg-brand-50/40'
          }
          ${isUploading ? 'pointer-events-none opacity-75' : ''}
        `}
      >
        <input {...getInputProps()} />

        {file ? (
          /* File selected state */
          <div className="flex flex-col items-center gap-3 animate-fade-in">
            <div className="w-14 h-14 rounded-xl bg-brand-100 flex items-center justify-center">
              <FileSpreadsheet className="w-7 h-7 text-brand-700" />
            </div>
            <div>
              <p className="font-semibold text-gray-900 text-sm">{file.name}</p>
              <p className="text-xs text-gray-500 mt-0.5">{formatBytes(file.size)}</p>
            </div>
            {!isUploading && (
              <button
                onClick={(e) => { e.stopPropagation(); handleReset() }}
                className="text-xs text-gray-400 hover:text-red-500 flex items-center gap-1 transition-colors"
              >
                <X className="w-3 h-3" /> Remove
              </button>
            )}
          </div>
        ) : (
          /* Empty state */
          <div className="flex flex-col items-center gap-3">
            <div className={`
              w-14 h-14 rounded-xl flex items-center justify-center transition-colors
              ${isDragActive ? 'bg-brand-200' : 'bg-gray-100'}
            `}>
              <Upload className={`w-7 h-7 transition-colors ${isDragActive ? 'text-brand-700' : 'text-gray-400'}`} />
            </div>
            <div>
              <p className="text-sm font-semibold text-gray-700">
                {isDragActive ? 'Drop it here' : 'Drop your SPIR file here'}
              </p>
              <p className="text-xs text-gray-400 mt-1">
                or click to browse — .xlsx, .xlsm accepted
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Error message */}
      {localError && (
        <div className="mt-3 flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 animate-fade-in">
          <AlertCircle className="w-4 h-4 text-red-500 mt-0.5 flex-shrink-0" />
          <p className="text-sm text-red-700">{localError}</p>
        </div>
      )}

      {/* Extract button */}
      {file && !isUploading && status !== 'success' && (
        <button
          onClick={handleExtract}
          className="
            mt-4 w-full py-3.5 px-6 rounded-xl font-semibold text-sm text-white
            bg-brand-600 hover:bg-brand-700 active:scale-[0.99]
            shadow-brand transition-all duration-150
            flex items-center justify-center gap-2
          "
        >
          <FileSpreadsheet className="w-4 h-4" />
          Extract SPIR Data
        </button>
      )}

      {/* New extraction button after success */}
      {status === 'success' && (
        <button
          onClick={handleReset}
          className="
            mt-4 w-full py-3 px-6 rounded-xl font-semibold text-sm
            text-brand-700 bg-brand-50 hover:bg-brand-100
            border border-brand-200 transition-all duration-150
          "
        >
          ↑ Extract another file
        </button>
      )}
    </div>
  )
})

UploadBox.displayName = 'UploadBox'
