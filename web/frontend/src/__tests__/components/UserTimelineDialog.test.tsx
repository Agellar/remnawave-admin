import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import client from '@/api/client'
import i18n from '@/i18n'
import { UserTimelineDialog } from '@/components/violations/UserTimelineDialog'

vi.mock('@/api/client', () => ({ default: { get: vi.fn() } }))

const get = vi.mocked(client.get)
const userA = '00000000-0000-4000-8000-000000000001'
const userB = '00000000-0000-4000-8000-000000000002'
const empty = { items: [], total: 0, pages: 1 }

function setup(uuid = userA) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const view = (userUuid: string | null) => (
    <QueryClientProvider client={cache}>
      <UserTimelineDialog userUuid={userUuid} onClose={vi.fn()} />
    </QueryClientProvider>
  )
  return { ...render(view(uuid)), view }
}

beforeEach(() => get.mockReset())

describe('UserTimelineDialog release hardening', () => {
  it('shows an error and retry instead of pretending history is empty', async () => {
    get.mockRejectedValueOnce(new Error('unavailable')).mockResolvedValue({ data: empty })
    setup()
    expect(await screen.findByRole('alert')).toHaveTextContent(i18n.t('violations.timeline.loadError'))
    expect(screen.queryByText(i18n.t('violations.timeline.empty'))).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: i18n.t('violations.timeline.retry') }))
    expect(await screen.findByText(i18n.t('violations.timeline.empty'))).toBeInTheDocument()
  })

  it('resets page before the next user request', async () => {
    get.mockResolvedValue({ data: { ...empty, total: 400, pages: 8 } })
    const mounted = setup()
    await userEvent.click(await screen.findByRole('button', { name: i18n.t('common.next') }))
    await waitFor(() => expect(get.mock.lastCall?.[1]?.params.page).toBe(2))
    get.mockResolvedValue({ data: empty })
    mounted.rerender(mounted.view(userB))
    await waitFor(() => expect(get.mock.lastCall?.[0]).toContain(userB))
    expect(get.mock.lastCall?.[1]?.params.page).toBe(1)
    expect(get.mock.lastCall?.[1]?.signal).toBeInstanceOf(AbortSignal)
  })

  it('resets the page on closing and reopening the same user', async () => {
    get.mockResolvedValue({ data: { ...empty, total: 400, pages: 8 } })
    const mounted = setup()
    await userEvent.click(await screen.findByRole('button', { name: i18n.t('common.next') }))
    await waitFor(() => expect(get.mock.lastCall?.[1]?.params.page).toBe(2))
    mounted.rerender(mounted.view(null))
    mounted.rerender(mounted.view(userA))
    await waitFor(() => expect(get.mock.lastCall?.[1]?.params.page).toBe(1))
  })

  it('marks annulled detections without a red guilty score', async () => {
    get.mockResolvedValue({ data: { total: 1, pages: 1, items: [{
      type: 'violation', id: 1, ts: '2026-08-28T12:00:00Z', score: 100,
      action: 'annulled', severity: 'critical', reasons: ['Old detector finding'],
    }] } })
    setup()
    const badge = await screen.findByText(i18n.t('violations.history.annulled'))
    expect(badge).toHaveClass('text-muted-foreground')
    expect(screen.queryByText(`${i18n.t('violations.timeline.violation')} · 100`)).not.toBeInTheDocument()
  })
})
