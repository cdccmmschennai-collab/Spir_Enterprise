import React, { memo, useState } from 'react'
import { Download, Loader2, CheckCircle2 } from 'lucide-react'
import { downloadFile } from '../services/api'

interface Props {
  fileId:   string
  filename: string
}

export const DownloadButton = memo(({ fileId, filename }: Props) => {
  const [state, setState] = useState<'idle' | 'loading' | 'done'>('idle')

  const handleDownload = async () => {
    if (state === 'loading') return
    setState('loading')
    try {
      await downloadFile(fileId, filename)
      setState('done')
      setTimeout(() => setState('idle'), 3000)
    } catch {
      setState('idle')
      // Fallback: open in new tab
      window.open(`http://127.0.0.1:8000/download/${fileId}`, '_blank')
    }
  }

  return (
    <button
      onClick={handleDownload}
      disabled={state === 'loading'}
      className={`
        group w-full py-4 px-6 rounded-xl font-semibold text-sm
        flex items-center justify-center gap-3
        transition-all duration-200 active:scale-[0.99]
        ${state === 'done'
          ? 'bg-brand-50 text-brand-700 border-2 border-brand-300'
          : 'bg-brand-600 hover:bg-brand-700 text-white shadow-brand hover:shadow-lg'
        }
        ${state === 'loading' ? 'opacity-80 cursor-not-allowed' : 'cursor-pointer'}
      `}
    >
      {state === 'loading' && (
        <Loader2 className="w-5 h-5 animate-spin" />
      )}
      {state === 'done' && (
        <CheckCircle2 className="w-5 h-5 text-brand-600" />
      )}
      {state === 'idle' && (
        <Download className="w-5 h-5 group-hover:translate-y-0.5 transition-transform" />
      )}

      <span>
        {state === 'loading' && 'Preparing download…'}
        {state === 'done'    && 'Download started!'}
        {state === 'idle'    && (
          <>
            Download Excel
            <span className="ml-2 text-xs font-normal opacity-75 font-mono">
              {filename}
            </span>
          </>
        )}
      </span>
    </button>
  )
})

DownloadButton.displayName = 'DownloadButton'
