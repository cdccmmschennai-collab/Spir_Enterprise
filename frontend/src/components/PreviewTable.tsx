import React, { memo, useMemo, useState } from 'react'
import type { ExtractResponse } from '../types/spir'

interface Props { result: ExtractResponse }

// Columns to show by default (most useful first)
const PRIORITY_COLS = [
  'TAG NO', 'DESCRIPTION OF PARTS', 'ITEM NUMBER', 'POSITION NUMBER',
  'OLD MATERIAL NUMBER/SPF NUMBER', 'QUANTITY IDENTICAL PARTS FITTED',
  'MANUFACTURER PART NUMBER', 'UNIT PRICE', 'UNIT PRICE (QAR)',
  'CURRENCY', 'SUPPLIER OCM NAME', 'SPIR TYPE', 'SHEET',
]

export const PreviewTable = memo(({ result }: Props) => {
  const [showAll, setShowAll] = useState(false)

  const cols = result.preview_cols ?? []
  const rows = result.preview_rows ?? []

  // Decide which column indices to display
  const visibleIndices = useMemo(() => {
    if (showAll) return cols.map((_, i) => i)
    // Show priority columns, then fill remaining up to 10
    const priority = PRIORITY_COLS.flatMap((name) => {
      const i = cols.indexOf(name)
      return i >= 0 ? [i] : []
    })
    if (priority.length === 0) return cols.map((_, i) => i).slice(0, 10)
    return priority.slice(0, 12)
  }, [cols, showAll])

  const totalRows = result.total_rows ?? rows.length

  if (!cols.length || !rows.length) {
    return (
      <div className="py-8 text-center text-sm text-gray-400">
        No preview data available.
      </div>
    )
  }

  return (
    <div className="animate-fade-up">
      {/* Header bar */}
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="font-display text-base font-semibold text-gray-900">
            Data preview
          </h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Showing {rows.length} of {totalRows.toLocaleString()} rows
            &nbsp;·&nbsp; {visibleIndices.length} of {cols.length} columns
          </p>
        </div>
        <button
          onClick={() => setShowAll(v => !v)}
          className="text-xs font-medium text-brand-600 hover:text-brand-800 transition-colors px-3 py-1.5 rounded-lg hover:bg-brand-50"
        >
          {showAll ? 'Show key columns' : `Show all ${cols.length} columns`}
        </button>
      </div>

      {/* Scrollable table */}
      <div className="relative overflow-auto rounded-xl border border-gray-100 shadow-card max-h-[420px]">
        <table className="min-w-full text-xs border-collapse">
          <thead className="sticky top-0 z-10">
            <tr>
              <th className="sticky left-0 z-20 bg-gray-900 text-white px-3 py-2.5 text-left font-semibold whitespace-nowrap w-8 border-r border-gray-700">
                #
              </th>
              {visibleIndices.map((ci) => (
                <th
                  key={ci}
                  className="bg-gray-900 text-white px-3 py-2.5 text-left font-semibold whitespace-nowrap border-r border-gray-700 last:border-r-0"
                  title={cols[ci]}
                >
                  <span className="block max-w-[140px] truncate">{cols[ci]}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIdx) => {
              const isDup = row[cols.indexOf('SPIR ERROR')] === 'DUPLICATE'
              return (
                <tr
                  key={rowIdx}
                  className={`
                    border-b border-gray-50 transition-colors
                    ${isDup ? 'bg-amber-50 hover:bg-amber-100' : rowIdx % 2 === 0 ? 'bg-white hover:bg-gray-50' : 'bg-gray-50/50 hover:bg-gray-100/50'}
                  `}
                >
                  {/* Row number */}
                  <td className="sticky left-0 bg-inherit px-3 py-2 text-gray-400 font-mono border-r border-gray-100 w-8 text-right">
                    {rowIdx + 1}
                  </td>
                  {visibleIndices.map((ci) => {
                    const val = row[ci]
                    const colName = cols[ci]

                    // Highlight specific columns
                    const isTag = colName === 'TAG NO'
                    const isSPF = colName === 'OLD MATERIAL NUMBER/SPF NUMBER'
                    const isPos = colName === 'POSITION NUMBER'
                    const isDupCell = colName === 'SPIR ERROR' && val === 'DUPLICATE'

                    return (
                      <td
                        key={ci}
                        className={`
                          px-3 py-2 border-r border-gray-100 last:border-r-0 max-w-[200px]
                          ${isTag  ? 'font-semibold text-brand-700' : ''}
                          ${isSPF  ? 'font-mono text-gray-700' : ''}
                          ${isPos  ? 'font-mono text-gray-600' : ''}
                          ${isDupCell ? 'text-amber-700 font-semibold' : ''}
                          ${!isTag && !isSPF && !isPos && !isDupCell ? 'text-gray-700' : ''}
                        `}
                        title={val ?? ''}
                      >
                        {val !== null && val !== undefined && val !== '' ? (
                          <span className="block truncate">{val}</span>
                        ) : (
                          <span className="text-gray-300">—</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* More rows note */}
      {totalRows > rows.length && (
        <p className="mt-2.5 text-center text-xs text-gray-400">
          + {(totalRows - rows.length).toLocaleString()} more rows in the downloaded Excel file
        </p>
      )}
    </div>
  )
})

PreviewTable.displayName = 'PreviewTable'
