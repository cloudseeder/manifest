const KEY = 'oap_agent_token'

export function getToken(): string {
  return localStorage.getItem(KEY) ?? ''
}

export function setToken(token: string): void {
  if (token) {
    localStorage.setItem(KEY, token)
  } else {
    localStorage.removeItem(KEY)
  }
}
