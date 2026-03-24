import React, { memo } from 'react'
import { Activity, LogOut, Zap } from 'lucide-react'
import { useStore } from '../store/useStore'
import { UploadBox }      from '../components/UploadBox'
import { ResultSummary }  from '../components/ResultSummary'
import { PreviewTable }   from '../components/PreviewTable'
import { DownloadButton } from '../components/DownloadButton'
import { Loader }         from '../components/Loader'
import { LoginModal }     from '../components/LoginModal'

// ── Header ────────────────────────────────────────────────────────────────────
const Header = memo(() => {
  const { token, setToken } = useStore()

  return (
    <header className="bg-gray-900 border-b border-gray-800 sticky top-0 z-40">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">

        {/* Logo + name */}
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-brand-600 flex items-center justify-center flex-shrink-0">
            <Zap className="w-4 h-4 text-white" />
          </div>
          <div className="flex items-baseline gap-2">
            <span className="font-display text-base font-semibold text-white">
              SPIR
            </span>
            <span className="text-xs text-gray-500 hidden sm:block">
              Enterprise Extraction
            </span>
          </div>
        </div>

        {/* Right side */}
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 text-xs text-gray-400">
            <Activity className="w-3.5 h-3.5 text-brand-500" />
            <span className="hidden sm:block">v2.0</span>
          </div>

          {token && (
            <button
              onClick={() => setToken(null)}
              className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white
                         transition-colors px-2.5 py-1.5 rounded-lg hover:bg-gray-800"
            >
              <LogOut className="w-3.5 h-3.5" />
              <span className="hidden sm:block">Sign out</span>
            </button>
          )}
        </div>
      </div>
    </header>
  )
})
Header.displayName = 'Header'

// ── Steps indicator ───────────────────────────────────────────────────────────
const STEPS = [
  { n: 1, label: 'Upload'    },
  { n: 2, label: 'Process'   },
  { n: 3, label: 'Preview'   },
  { n: 4, label: 'Download'  },
]

const StepsBar = memo(({ active }: { active: number }) => (
  <div className="flex items-center gap-0 mb-8">
    {STEPS.map((step, idx) => (
      <React.Fragment key={step.n}>
        <div className="flex items-center gap-2">
          <div className={`
            w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold
            transition-all duration-300
            ${step.n <= active
              ? 'bg-brand-600 text-white'
              : 'bg-gray-100 text-gray-400'
            }
          `}>
            {step.n < active ? '✓' : step.n}
          </div>
          <span className={`text-xs font-medium hidden sm:block transition-colors ${
            step.n <= active ? 'text-gray-900' : 'text-gray-400'
          }`}>
            {step.label}
          </span>
        </div>
        {idx < STEPS.length - 1 && (
          <div className={`
            flex-1 mx-2 h-px transition-all duration-500
            ${step.n < active ? 'bg-brand-400' : 'bg-gray-200'}
          `} />
        )}
      </React.Fragment>
    ))}
  </div>
))
StepsBar.displayName = 'StepsBar'

// ── Dashboard ─────────────────────────────────────────────────────────────────
export const Dashboard = memo(() => {
  const { token, status, progress, result, filename } = useStore()

  const activeStep = (() => {
    if (!result)                          return status === 'uploading' ? 2 : 1
    if (result && !result.file_id)        return 3
    return 4
  })()

  // Filename for loader display (stored in Zustand state)
  const uploadingFile = filename || 'Processing…'

  return (
    <>
      {/* Show login modal if not authenticated */}
      {/* Auth disabled */}

      <div className="min-h-screen bg-gray-50">
        <Header />

        <main className="max-w-5xl mx-auto px-4 sm:px-6 py-8 sm:py-10">

          {/* Page title */}
          <div className="mb-8">
            <h1 className="font-display text-2xl sm:text-3xl font-bold text-gray-900 tracking-tight">
              Spare Parts Extraction
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Upload a SPIR Excel file to extract, normalize, and download structured spare parts data.
            </p>
          </div>

          <StepsBar active={activeStep} />

          {/* ── STEP 1: Upload ─────────────────────────────────────────────── */}
          <section className="card-padded mb-5">
            <div className="flex items-center gap-2 mb-5">
              <span className="w-6 h-6 rounded-full bg-gray-900 text-white text-xs
                               flex items-center justify-center font-semibold">1</span>
              <h2 className="font-display text-base font-semibold text-gray-900">
                Upload SPIR file
              </h2>
              <span className="ml-auto text-xs text-gray-400 font-mono">
                .xlsx · .xlsm
              </span>
            </div>

            {status === 'uploading' ? (
              <Loader progress={progress} filename={uploadingFile} />
            ) : (
              <UploadBox />
            )}
          </section>

          {/* ── STEPS 2-4: Result ──────────────────────────────────────────── */}
          {result && status === 'success' && (
            <>
              {/* Step 2: Summary */}
              <section className="card-padded mb-5">
                <div className="flex items-center gap-2 mb-5">
                  <span className="w-6 h-6 rounded-full bg-brand-600 text-white text-xs
                                   flex items-center justify-center font-semibold">2</span>
                  <h2 className="font-display text-base font-semibold text-gray-900">
                    Extraction summary
                  </h2>
                </div>
                <ResultSummary result={result} />
              </section>

              {/* Step 3: Preview table */}
              <section className="card-padded mb-5">
                <div className="flex items-center gap-2 mb-5">
                  <span className="w-6 h-6 rounded-full bg-brand-600 text-white text-xs
                                   flex items-center justify-center font-semibold">3</span>
                  <h2 className="font-display text-base font-semibold text-gray-900">
                    Data preview
                  </h2>
                  <span className="ml-auto text-xs text-gray-400">
                    {(result.total_rows ?? 0).toLocaleString()} total rows
                  </span>
                </div>
                <PreviewTable result={result} />
              </section>

              {/* Step 4: Download */}
              {result.file_id && result.filename && (
                <section className="card-padded">
                  <div className="flex items-center gap-2 mb-5">
                    <span className="w-6 h-6 rounded-full bg-brand-600 text-white text-xs
                                     flex items-center justify-center font-semibold">4</span>
                    <h2 className="font-display text-base font-semibold text-gray-900">
                      Download result
                    </h2>
                  </div>
                  <DownloadButton
                    fileId={result.file_id}
                    filename={result.filename}
                  />
                  <p className="text-xs text-gray-400 text-center mt-3">
                    Full {(result.total_rows ?? 0).toLocaleString()}-row Excel file
                    with all {(result.preview_cols?.length ?? 0)} columns,
                    SPF numbers, position numbers, and currency conversion.
                  </p>
                </section>
              )}
            </>
          )}

          {/* ── Footer ────────────────────────────────────────────────────── */}
          <footer className="mt-12 pb-6 text-center text-xs text-gray-400 space-x-4">
            <span>SPIR Enterprise v2.0</span>
            <span>·</span>
            <a
              href="http://127.0.0.1:8000/api/docs"
              target="_blank"
              rel="noreferrer"
              className="hover:text-brand-600 transition-colors"
            >
              API Docs
            </a>
            <span>·</span>
            <span>FORMAT1–FORMAT8 + Adaptive</span>
          </footer>
        </main>
      </div>
    </>
  )
})

Dashboard.displayName = 'Dashboard'
