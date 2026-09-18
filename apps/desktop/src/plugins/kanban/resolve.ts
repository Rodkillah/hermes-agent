/** Shared helpers for the block-loop decision surface. */

const STATE_EVENT_KINDS = new Set([
  'archived',
  'block_loop_detected',
  'blocked',
  'changes_requested',
  'completed',
  'dependency_wait',
  'promoted',
  'review_reopened',
  'review_requested',
  'status',
  'unblocked'
])

/**
 * Return the id of the current block-loop detection in a task detail snapshot.
 * The id, rather than the array position, is the CAS token sent back to the
 * domain endpoint; a reordered or partially refreshed event list cannot make
 * the UI silently target an older loop. This mirrors the domain's state-event
 * ordering, so a historical detection is not offered as a live decision.
 */
export function latestBlockLoopEventId(events: readonly { id: number; kind: string }[]): null | number {
  let latestState: null | { id: number; kind: string } = null

  for (const event of events) {
    if (STATE_EVENT_KINDS.has(event.kind) && (latestState === null || event.id > latestState.id)) {
      latestState = event
    }
  }

  return latestState?.kind === 'block_loop_detected' ? latestState.id : null
}
