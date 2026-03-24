import React, { memo } from 'react'

interface LoaderProps {
  progress: number
  filename: string
}

const STEPS = [
  'Validating file structure…',
  'Detecting SPIR format…',
  'Extracting spare parts…',
  'Splitting multi-tag rows…',
  'Computing SPF numbers…',
  'Calculating position numbers…',
  'Converting currencies to INR…',
  'Checking duplicates…',
  'Building Excel output…',
  'Finalising result…',
]

export const Loader = memo(({ progress, filename }: LoaderProps) => {
  const stepIndex = Math.min(Math.floor((progress / 100) * STEPS.length), STEPS.length - 1)
  const step      = STEPS[stepIndex]

  return (
    <div className="flex flex-col items-center justify-center py-16 animate-fade-in">
      {/* Animated ring */}
      <div className="relative w-20 h-20 mb-6">
        <svg className="w-full h-full -rotate-90" viewBox="0 0 80 80">
          <circle
            cx="40" cy="40" r="34"
            fill="none"
            stroke="#e5e7eb"
            strokeWidth="5"
          />
          <circle
            cx="40" cy="40" r="34"
            fill="none"
            stroke="#16a34a"
            strokeWidth="5"
            strokeLinecap="round"
            strokeDasharray={`${2 * Math.PI * 34}`}
            strokeDashoffset={`${2 * Math.PI * 34 * (1 - progress / 100)}`}
            className="transition-all duration-500 ease-out"
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="font-mono text-sm font-semibold text-brand-700">
            {progress}%
          </span>
        </div>
      </div>

      <h3 className="font-display text-lg font-semibold text-gray-900 mb-1">
        Processing SPIR
      </h3>
      <p className="text-sm text-gray-500 mb-1 font-mono truncate max-w-xs text-center">
        {filename}
      </p>
      <p className="text-xs text-brand-600 font-medium mt-2 animate-pulse-slow">
        {step}
      </p>

      {/* Step bar */}
      <div className="flex gap-1 mt-5">
        {STEPS.map((_, i) => (
          <div
            key={i}
            className={`h-1 w-5 rounded-full transition-all duration-300 ${
              i <= stepIndex ? 'bg-brand-500' : 'bg-gray-200'
            }`}
          />
        ))}
      </div>
    </div>
  )
})

Loader.displayName = 'Loader'
