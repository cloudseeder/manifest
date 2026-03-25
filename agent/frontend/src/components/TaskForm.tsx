import { useState, useEffect } from 'react'
import type { AgentTask } from '@/lib/types'
import CronInput from './CronInput'

interface TaskFormProps {
  task?: AgentTask
  onSave: (task: AgentTask) => void
  onCancel: () => void
}

export default function TaskForm({ task, onSave, onCancel }: TaskFormProps) {
  const [name, setName] = useState(task?.name || '')
  const [prompt, setPrompt] = useState(task?.prompt || task?.user_prompt || '')
  const [schedule, setSchedule] = useState(task?.schedule || '')
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState(task?.model || '')
  const [incremental, setIncremental] = useState(task?.incremental ?? true)
  const [autoRefine, setAutoRefine] = useState(false)

  // Refinement state
  const [refining, setRefining] = useState(false)
  const [refinedPrompt, setRefinedPrompt] = useState<string | null>(null)
  const [refinedModel, setRefinedModel] = useState<string | null>(null)
  const [escalationEnabled, setEscalationEnabled] = useState(false)

  useEffect(() => {
    fetch('/v1/agent/models')
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data) {
          setModels(data.models || [])
          if (!model) setModel(task?.model || data.default || data.models?.[0] || '')
        }
      })
      .catch(() => {})
    // Check if escalation (big LLM) is available
    fetch('/v1/agent/health')
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data?.escalation_enabled) setEscalationEnabled(true)
      })
      .catch(() => {})
  }, [])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleRefine() {
    if (!prompt.trim() || refining) return
    setRefining(true)
    setRefinedPrompt(null)
    try {
      const res = await fetch('/v1/agent/tasks/refine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: prompt.trim() }),
      })
      if (res.ok) {
        const data = await res.json()
        if (data.refined && data.refined !== prompt.trim()) {
          setRefinedPrompt(data.refined)
          setRefinedModel(data.model || null)
        } else {
          setRefinedPrompt(null)
          setError('Prompt is already well-optimized')
          setTimeout(() => setError(null), 3000)
        }
      }
    } catch {
      setError('Refinement failed')
    } finally {
      setRefining(false)
    }
  }

  function acceptRefinement() {
    if (refinedPrompt) {
      setPrompt(refinedPrompt)
      setRefinedPrompt(null)
      setAutoRefine(false) // already refined manually
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || !prompt.trim()) return

    setSaving(true)
    setError(null)

    const body = {
      name: name.trim(),
      prompt: prompt.trim(),
      schedule: schedule.trim() || undefined,
      model,
      incremental,
      auto_refine: autoRefine && escalationEnabled,
    }

    try {
      const url = task ? `/v1/agent/tasks/${task.id}` : '/v1/agent/tasks'
      const method = task ? 'PATCH' : 'POST'
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        setError(data.error || 'Failed to save task')
        return
      }
      const data = await res.json()
      onSave(data.task || data)
    } catch {
      setError('Failed to save task')
    } finally {
      setSaving(false)
    }
  }

  // Show original user prompt if editing a refined task
  const hasRefinedPrompt = task?.user_prompt && task.user_prompt !== task.prompt
  const [showOriginal, setShowOriginal] = useState(false)

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          placeholder="Daily news summary"
          className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        />
      </div>

      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="block text-sm font-medium text-gray-700">Prompt</label>
          {escalationEnabled && prompt.trim().length > 5 && (
            <button
              type="button"
              onClick={handleRefine}
              disabled={refining || saving}
              className="text-xs text-primary hover:underline disabled:opacity-50"
            >
              {refining ? 'Refining...' : 'Refine with AI'}
            </button>
          )}
        </div>
        <textarea
          value={prompt}
          onChange={(e) => { setPrompt(e.target.value); setRefinedPrompt(null) }}
          required
          rows={4}
          placeholder="Describe what this task should do..."
          className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        />

        {/* Refinement suggestion */}
        {refinedPrompt && (
          <div className="mt-2 rounded-md border border-blue-200 bg-blue-50 p-3">
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-blue-700">
                Suggested refinement {refinedModel && <span className="font-normal text-blue-500">via {refinedModel}</span>}
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={acceptRefinement}
                  className="text-xs font-medium text-blue-700 hover:underline"
                >
                  Accept
                </button>
                <button
                  type="button"
                  onClick={() => setRefinedPrompt(null)}
                  className="text-xs text-gray-500 hover:underline"
                >
                  Dismiss
                </button>
              </div>
            </div>
            <p className="text-sm text-blue-900 whitespace-pre-wrap">{refinedPrompt}</p>
          </div>
        )}

        {/* Show original prompt for refined tasks */}
        {hasRefinedPrompt && (
          <div className="mt-1">
            <button
              type="button"
              onClick={() => setShowOriginal(!showOriginal)}
              className="text-xs text-gray-500 hover:underline"
            >
              {showOriginal ? 'Hide' : 'Show'} original prompt
            </button>
            {showOriginal && (
              <p className="mt-1 rounded-md bg-gray-50 p-2 text-xs text-gray-600 whitespace-pre-wrap">
                {task?.user_prompt}
              </p>
            )}
          </div>
        )}
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Schedule (cron)</label>
        <CronInput value={schedule} onChange={setSchedule} disabled={saving} />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Model</label>
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        >
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </div>

      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="incremental"
            checked={incremental}
            onChange={(e) => setIncremental(e.target.checked)}
            className="rounded border-gray-300 text-primary focus:ring-primary"
          />
          <label htmlFor="incremental" className="text-sm text-gray-700">
            Incremental — only include new information since last run
          </label>
        </div>
        {escalationEnabled && (
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="auto-refine"
              checked={autoRefine}
              onChange={(e) => setAutoRefine(e.target.checked)}
              className="rounded border-gray-300 text-primary focus:ring-primary"
            />
            <label htmlFor="auto-refine" className="text-sm text-gray-700">
              Auto-refine prompt on save
            </label>
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="flex justify-end gap-2 pt-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={saving || !name.trim() || !prompt.trim()}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-600 disabled:opacity-50"
        >
          {saving ? (autoRefine && escalationEnabled ? 'Refining & Saving...' : 'Saving...') : task ? 'Save Changes' : 'Create Task'}
        </button>
      </div>
    </form>
  )
}
