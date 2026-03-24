import React, { memo, useState } from 'react'
import { Lock, Loader2, AlertCircle, Eye, EyeOff } from 'lucide-react'
import { login } from '../services/api'
import { useStore } from '../store/useStore'

export const LoginModal = memo(() => {
  const setToken   = useStore(s => s.setToken)
  const [user, setUser]   = useState('admin')
  const [pass, setPass]   = useState('')
  const [show, setShow]   = useState(false)
  const [busy, setBusy]   = useState(false)
  const [err,  setErr]    = useState<string | null>(null)

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!user || !pass) { setErr('Enter username and password.'); return }
    setBusy(true)
    setErr(null)
    try {
      // TEMP: bypass auth completely
      setToken("dev-token")
    } catch {
      setToken("dev-token")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-sm mx-4 bg-white rounded-2xl shadow-2xl border border-gray-100 overflow-hidden animate-fade-up">

        {/* Header */}
        <div className="bg-gray-900 px-7 py-6 text-white">
          <div className="w-10 h-10 rounded-xl bg-brand-600 flex items-center justify-center mb-4">
            <Lock className="w-5 h-5" />
          </div>
          <h2 className="font-display text-xl font-semibold">Sign in</h2>
          <p className="text-sm text-gray-400 mt-1">SPIR Enterprise Extraction Tool</p>
        </div>

        {/* Form */}
        <form onSubmit={handleLogin} className="px-7 py-6 space-y-4">
          <div>
            <label className="label-xs block mb-1.5">Username</label>
            <input
              type="text"
              value={user}
              onChange={e => setUser(e.target.value)}
              autoComplete="username"
              className="
                w-full px-3.5 py-2.5 text-sm rounded-xl border border-gray-200
                focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                transition-all placeholder-gray-300
              "
              placeholder="admin"
            />
          </div>

          <div>
            <label className="label-xs block mb-1.5">Password</label>
            <div className="relative">
              <input
                type={show ? 'text' : 'password'}
                value={pass}
                onChange={e => setPass(e.target.value)}
                autoComplete="current-password"
                className="
                  w-full px-3.5 py-2.5 pr-10 text-sm rounded-xl border border-gray-200
                  focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                  transition-all placeholder-gray-300
                "
                placeholder="••••••••"
              />
              <button
                type="button"
                onClick={() => setShow(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
              >
                {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>

          {err && (
            <div className="flex items-center gap-2 p-3 rounded-xl bg-red-50 border border-red-100">
              <AlertCircle className="w-4 h-4 text-red-500 flex-shrink-0" />
              <p className="text-sm text-red-700">{err}</p>
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="btn-primary w-full justify-center disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin" /> Signing in…</>
              : 'Sign in'
            }
          </button>

          <p className="text-center text-xs text-gray-400">
            Default credentials: <span className="font-mono">admin / admin123</span>
          </p>
        </form>
      </div>
    </div>
  )
})

LoginModal.displayName = 'LoginModal'
