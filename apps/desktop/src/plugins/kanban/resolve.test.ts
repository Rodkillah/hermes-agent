import { describe, expect, it } from 'vitest'

import { latestBlockLoopEventId } from './resolve'

describe('latestBlockLoopEventId', () => {
  it('returns the newest detection id from a task detail snapshot', () => {
    expect(
      latestBlockLoopEventId([
        { id: 12, kind: 'block_loop_detected' },
        { id: 13, kind: 'block_loop_resolved' },
        { id: 27, kind: 'block_loop_detected' },
        { id: 28, kind: 'commented' }
      ])
    ).toBe(27)
  })

  it('returns null when no block-loop detection is present', () => {
    expect(
      latestBlockLoopEventId([
        { id: 1, kind: 'created' },
        { id: 2, kind: 'blocked' }
      ])
    ).toBeNull()
  })

  it('does not offer a historical detection after a newer state transition', () => {
    expect(
      latestBlockLoopEventId([
        { id: 10, kind: 'block_loop_detected' },
        { id: 11, kind: 'status' }
      ])
    ).toBeNull()
  })
})
