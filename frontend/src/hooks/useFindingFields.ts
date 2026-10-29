import { useCallback, useEffect, useState } from 'react'

import { operationsApi } from '@/services/api'
import { toApiError } from '@/services/errors'
import type { FindingFields } from '@/types'

interface FindingFieldsState {
  /** Null until the fields load. */
  readonly fields: FindingFields | null
  /** The last load failure, or an empty string. */
  readonly error: string
  readonly retry: () => void
}

/**
 * Loads the active finding fields, for the finding dialog, cards and counts.
 * @param enabled - Loads only while true, and again each time it turns true, so a closed dialog fetches nothing.
 */
export function useFindingFields(enabled = true): FindingFieldsState {
  const [fields, setFields] = useState<FindingFields | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!enabled) return
    let ignore = false
    operationsApi.getFindingFields()
      .then((loaded: FindingFields) => {
        if (!ignore) {
          setFields(loaded)
          setError('')
        }
      })
      .catch((cause: unknown) => {
        if (!ignore) setError(toApiError(cause).detail)
      })
    return () => {
      ignore = true
    }
  }, [attempt, enabled])

  const retry = useCallback(() => {
    setError('')
    setAttempt((count: number) => count + 1)
  }, [])

  return { fields, error, retry }
}
