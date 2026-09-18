import { afterEach, describe, expect, it, vi } from 'vitest'

import { bindApi, resolveBlockLoopTask } from './api'

const { atomMock, queryClientMock } = vi.hoisted(() => {
  const atomMock = <T>(initial: T) => {
    let value = initial
    const listeners = new Set<(next: T) => void>()

    return {
      get: () => value,
      listen: (listener: (next: T) => void) => {
        listeners.add(listener)

        return () => listeners.delete(listener)
      },
      set: (next: T) => {
        value = next
        listeners.forEach(listener => listener(next))
      }
    }
  }

  return { atomMock, queryClientMock: { invalidateQueries: vi.fn() } }
})

vi.mock('@hermes/plugin-sdk', () => ({
  atom: atomMock,
  queryClient: queryClientMock
}))

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('resolveBlockLoopTask API', () => {
  it('sends the caller-provided event id as the CAS token', async () => {
    vi.useFakeTimers()
    const rest = vi.fn(async () => ({ ok: true }))
    const storage = { get: <T>(_key: string, fallback: T) => fallback, set: vi.fn() }
    const socket = vi.fn(() => vi.fn())
    const dispose = bindApi(rest as never, storage as never, socket as never)

    await resolveBlockLoopTask('t_loop', {
      actor: 'desktop',
      decision: 'archive',
      expected_event_id: 42,
      reason: 'superseded'
    })

    expect(rest).toHaveBeenCalledWith('/tasks/t_loop/resolve-block-loop', {
      body: {
        actor: 'desktop',
        decision: 'archive',
        expected_event_id: 42,
        reason: 'superseded'
      },
      method: 'POST'
    })

    dispose()
  })
})
