import React, { memo } from 'react'
import {
  Hash, Tag, Layers, Factory, Truck, FileText,
  AlertTriangle, CheckCircle2, Info
} from 'lucide-react'
import type { ExtractResponse } from '../types/spir'

interface Props { result: ExtractResponse }

interface StatCardProps {
  icon: React.ReactNode
  label: string
  value: string | number | null | undefined
  accent?: boolean
  warn?: boolean
}

const StatCard = memo(({ icon, label, value, accent, warn }: StatCardProps) => (
  <div className={`
    flex items-center gap-3 p-4 rounded-xl border transition-all
    ${accent ? 'bg-brand-50 border-brand-200' : ''}
    ${warn   ? 'bg-amber-50 border-amber-200' : ''}
    ${!accent && !warn ? 'bg-white border-gray-100 shadow-card' : ''}
  `}>
    <div className={`
      w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0
      ${accent ? 'bg-brand-100 text-brand-700' : ''}
      ${warn   ? 'bg-amber-100 text-amber-700' : ''}
      ${!accent && !warn ? 'bg-gray-100 text-gray-500' : ''}
    `}>
      {icon}
    </div>
    <div className="min-w-0">
      <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">{label}</p>
      <p className={`
        text-sm font-semibold mt-0.5 truncate
        ${accent ? 'text-brand-800' : ''}
        ${warn   ? 'text-amber-800' : ''}
        ${!accent && !warn ? 'text-gray-900' : ''}
      `}>
        {value ?? '—'}
      </p>
    </div>
  </div>
))

StatCard.displayName = 'StatCard'

export const ResultSummary = memo(({ result }: Props) => {
  const hasDups = (result.dup1_count ?? 0) > 0

  return (
    <div className="animate-fade-up">
      {/* Status banner */}
      <div className="flex items-center gap-2 mb-5 p-3.5 rounded-xl bg-brand-600 text-white">
        <CheckCircle2 className="w-5 h-5 flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="font-semibold text-sm">Extraction complete</p>
          <p className="text-xs text-brand-100 font-mono truncate">
            {result.spir_no || result.filename || 'SPIR file processed'}
          </p>
        </div>
        <span className="text-xs font-mono bg-brand-700 px-2 py-1 rounded-lg whitespace-nowrap">
          {result.format}
        </span>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
        <StatCard
          icon={<Hash className="w-4 h-4" />}
          label="Total Rows"
          value={result.total_rows?.toLocaleString()}
          accent
        />
        <StatCard
          icon={<Tag className="w-4 h-4" />}
          label="Tags"
          value={result.total_tags}
          accent
        />
        <StatCard
          icon={<Layers className="w-4 h-4" />}
          label="Spare Items"
          value={result.spare_items}
          accent
        />
        <StatCard
          icon={<FileText className="w-4 h-4" />}
          label="SPIR No"
          value={result.spir_no}
        />
        <StatCard
          icon={<Factory className="w-4 h-4" />}
          label="Manufacturer"
          value={result.manufacturer}
        />
        <StatCard
          icon={<Truck className="w-4 h-4" />}
          label="Supplier"
          value={result.supplier}
        />
        {result.spir_type && (
          <StatCard
            icon={<Info className="w-4 h-4" />}
            label="SPIR Type"
            value={result.spir_type}
          />
        )}
        {result.eqpt_qty != null && (
          <StatCard
            icon={<Hash className="w-4 h-4" />}
            label="Eqpt Qty"
            value={result.eqpt_qty}
          />
        )}
        {hasDups && (
          <StatCard
            icon={<AlertTriangle className="w-4 h-4" />}
            label="Duplicates"
            value={result.dup1_count}
            warn
          />
        )}
      </div>

      {/* Annexure stats */}
      {result.annexure_stats && Object.keys(result.annexure_stats).length > 0 && (
        <div className="mt-4 p-3.5 rounded-xl border border-gray-100 bg-gray-50">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
            Sheet breakdown
          </p>
          <div className="flex flex-wrap gap-2">
            {Object.entries(result.annexure_stats).map(([sheet, count]) => (
              <span
                key={sheet}
                className="text-xs px-2.5 py-1 rounded-full bg-white border border-gray-200 text-gray-700 font-medium"
              >
                {sheet} <span className="text-brand-600 font-semibold">{count}</span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
})

ResultSummary.displayName = 'ResultSummary'
