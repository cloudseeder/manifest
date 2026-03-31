import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import App from './App'
import './index.css'
import { getToken } from './lib/token'

// Patch global fetch to inject bearer token on all /v1/ API calls.
// This keeps auth transparent to all components.
const _fetch = window.fetch.bind(window)
window.fetch = (input, init) => {
  const token = getToken()
  if (!token) return _fetch(input, init)
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : (input as Request).url
  if (!url.startsWith('/v1/')) return _fetch(input, init)
  const headers = new Headers((init?.headers as HeadersInit | undefined) ?? {})
  headers.set('Authorization', `Bearer ${token}`)
  return _fetch(input, { ...init, headers })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
