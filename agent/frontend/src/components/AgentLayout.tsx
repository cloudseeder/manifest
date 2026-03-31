import { useCallback, useEffect, useRef, useState } from 'react'
import { Outlet } from 'react-router'
import AgentSidebar from './AgentSidebar'
import AgentEventProvider, { useAgentEvents } from './AgentEventProvider'
import { AvatarStateContext, type AvatarState } from '@/hooks/useAvatarState'
import { subscribeSpeaking, useAnySpeaking } from '@/hooks/useTTS'
import PersonaAvatar from './PersonaAvatar'

/** Inner component that can access AgentEventProvider context for broadcasting. */
function AgentLayoutInner() {
  const [avatarState, setAvatarState] = useState<AvatarState>({
    recording: false,
    streaming: false,
    attentive: false,
    persona: '',
  })
  const stateRef = useRef(avatarState)
  stateRef.current = avatarState

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const anySpeaking = useAnySpeaking()

  const update = useCallback((patch: Partial<AvatarState>) => {
    setAvatarState((prev) => ({ ...prev, ...patch }))
  }, [])

  const { notificationCount } = useAgentEvents()
  const notifRef = useRef(notificationCount)
  notifRef.current = notificationCount

  const speakingRef = useRef(false)
  const channelRef = useRef<BroadcastChannel | null>(null)

  const broadcast = useCallback(() => {
    const s = stateRef.current
    channelRef.current?.postMessage({
      recording: s.recording,
      streaming: s.streaming,
      speaking: speakingRef.current,
      persona: s.persona,
      hasNotifications: notifRef.current > 0,
    })
  }, [])

  useEffect(() => {
    channelRef.current = new BroadcastChannel('oap-avatar')
    const unsub = subscribeSpeaking((v) => {
      speakingRef.current = v
      broadcast()
    })
    return () => { unsub(); channelRef.current?.close(); channelRef.current = null }
  }, [broadcast])

  useEffect(() => {
    broadcast()
  }, [avatarState.recording, avatarState.streaming, avatarState.persona, notificationCount, broadcast])

  return (
    <AvatarStateContext.Provider value={{ state: avatarState, update }}>
      <div className="flex h-screen overflow-hidden bg-white">

        {/* Mobile backdrop */}
        {sidebarOpen && (
          <div
            className="fixed inset-0 z-30 bg-black/50 sm:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar — always visible on sm+, drawer on mobile */}
        <div className={`
          fixed inset-y-0 left-0 z-40 transition-transform duration-300 ease-in-out
          sm:relative sm:z-auto sm:translate-x-0
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
        `}>
          <AgentSidebar onClose={() => setSidebarOpen(false)} />
        </div>

        <main className="flex flex-1 flex-col overflow-hidden min-w-0">
          {/* Mobile header bar */}
          <header className="flex h-24 shrink-0 items-center justify-between border-b border-gray-200 px-3 sm:hidden">
            <button
              onClick={() => setSidebarOpen(true)}
              className="flex h-10 w-10 items-center justify-center rounded-lg text-gray-600 hover:bg-gray-100"
              aria-label="Open menu"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <div className="relative">
              <PersonaAvatar
                persona={avatarState.persona}
                speaking={anySpeaking}
                recording={avatarState.recording}
                streaming={avatarState.streaming}
                attentive={avatarState.attentive}
                hasNotifications={notificationCount > 0}
                size={88}
                audioLevelRef={avatarState.audioLevelRef}
              />
              {notificationCount > 0 && (
                <span className="absolute -top-1 -right-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-0.5 text-[10px] font-bold text-white shadow">
                  {notificationCount > 99 ? '99+' : notificationCount}
                </span>
              )}
            </div>
            <div className="w-10" />{/* spacer to center avatar */}
          </header>

          <Outlet />
        </main>
      </div>
    </AvatarStateContext.Provider>
  )
}

export default function AgentLayout() {
  return (
    <AgentEventProvider>
      <AgentLayoutInner />
    </AgentEventProvider>
  )
}
