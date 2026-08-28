/**
 * UserTimelineDialog — единая лента событий пользователя:
 * нарушения + сессии подключений + HWID-устройства, в хронологии.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import client from '@/api/client'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { ShieldAlert, Wifi, Smartphone, Clock } from '@/components/brand/icons'
import { cn } from '@/lib/utils'

const PER_PAGE = 50

interface TimelineEvent {
  type: 'violation' | 'connection' | 'hwid'
  ts: string | null
  // violation
  id?: number
  score?: number
  severity?: string
  action?: string
  reasons?: string[]
  // connection
  ip?: string
  node_name?: string | null
  disconnected_at?: string | null
  platform?: string | null
  user_agent?: string | null
  // hwid
  hwid?: string
  device_model?: string | null
  app_version?: string | null
}

const SEV_COLOR: Record<string, string> = {
  critical: 'text-red-400', high: 'text-orange-400', medium: 'text-yellow-400', low: 'text-blue-400',
}

function fmtTs(ts: string | null): string {
  if (!ts) return '—'
  return ts.slice(0, 16).replace('T', ' ')
}

function EventRow({ e, t }: { e: TimelineEvent; t: (k: string, o?: Record<string, unknown>) => string }) {
  const annulled = e.type === 'violation' && e.action === 'annulled'
  const icon = e.type === 'violation'
    ? <ShieldAlert className={cn('w-4 h-4', annulled ? 'text-muted-foreground' : SEV_COLOR[e.severity || ''] || 'text-red-400')} />
    : e.type === 'connection'
      ? <Wifi className="w-4 h-4 text-primary-400" />
      : <Smartphone className="w-4 h-4 text-violet-400" />

  return (
    <div className="flex gap-3">
      <div className="flex flex-col items-center">
        <div className="w-7 h-7 rounded-full bg-[var(--glass-bg)] border border-[var(--glass-border)] flex items-center justify-center shrink-0">
          {icon}
        </div>
        <div className="flex-1 w-px bg-[var(--glass-border)] my-1" />
      </div>
      <div className="pb-4 min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[11px] text-muted-foreground font-mono">{fmtTs(e.ts)}</span>
          {e.type === 'violation' && (
            <Badge className={cn('text-xs', annulled ? 'bg-muted text-muted-foreground' : ['bg-red-500/15', SEV_COLOR[e.severity || '']])}>
              {annulled ? t('violations.history.annulled') : `${t('violations.timeline.violation')} · ${Math.round(e.score || 0)}`}
            </Badge>
          )}
          {e.type === 'connection' && (
            <Badge variant="outline" className="text-[10px]">{t('violations.timeline.connection')}</Badge>
          )}
          {e.type === 'hwid' && (
            <Badge variant="outline" className="text-[10px] text-violet-300">{t('violations.timeline.hwid')}</Badge>
          )}
        </div>
        <div className="text-xs text-foreground/80 mt-0.5 break-words">
          {e.type === 'violation' && (e.reasons?.length ? e.reasons.slice(0, 3).join(' · ') : (e.action || '—'))}
          {e.type === 'connection' && (
            <span>{e.ip}{e.node_name ? ` → ${e.node_name}` : ''}{e.platform ? ` · ${e.platform}` : ''}</span>
          )}
          {e.type === 'hwid' && (
            <span className="font-mono">{e.platform || '?'}{e.device_model ? ` · ${e.device_model}` : ''}{e.app_version ? ` · v${e.app_version}` : ''}</span>
          )}
        </div>
      </div>
    </div>
  )
}

type UserTimelineProps = {
  userUuid: string | null; username?: string; onClose: () => void
}

export function UserTimelineDialog(props: UserTimelineProps) {
  // Reset page and period before the next user's first request, including
  // reopening a closed dialog; never flash a page belonging to a prior user.
  return <UserTimelineContent key={props.userUuid ?? 'closed'} {...props} />
}

function UserTimelineContent({ userUuid, username, onClose }: UserTimelineProps) {
  const { t } = useTranslation()
  const [days, setDays] = useState(30)
  const [page, setPage] = useState(1)

  // Смена периода начинает чтение заново: третья страница месяца не имеет
  // отношения к третьей странице суток.
  const pickDays = (d: number) => { setDays(d); setPage(1) }

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['user-timeline', userUuid, days, page],
    queryFn: async ({ signal }) => (await client.get(`/violations/user/${userUuid}/timeline`, {
      params: { days, page, per_page: PER_PAGE },
      signal,
    })).data as { items: TimelineEvent[]; total: number; pages: number },
    enabled: !!userUuid,
  })
  const items = data?.items || []
  const pages = data?.pages || 1
  const total = data?.total || 0

  return (
    <Dialog open={userUuid !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg max-h-[85vh] flex flex-col" aria-describedby={undefined}>
        <DialogHeader className="shrink-0">
          <div className="flex items-center gap-2 flex-wrap pr-8">
            <Clock className="w-5 h-5 text-primary-400" />
            <DialogTitle className="text-base">{t('violations.timeline.title')}</DialogTitle>
            {username && <span className="text-sm text-muted-foreground">{username}</span>}
          </div>
          <div className="flex items-center gap-1 mt-1">
            {[1, 7, 30, 90].map((d) => (
              <button key={d} type="button" onClick={() => pickDays(d)} aria-pressed={days === d}
                className={cn('min-h-8 px-2 py-0.5 rounded-md text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-400',
                  days === d ? 'bg-primary-500/20 text-primary-300' : 'text-muted-foreground hover:text-white')}>
                {d}{t('violations.timeline.daysShort')}
              </button>
            ))}
          </div>
        </DialogHeader>
        <div className="flex-1 min-h-0 overflow-y-auto -mr-2 pr-2" aria-busy={isFetching}>
          {isLoading ? (
            <div className="space-y-3">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>
          ) : isError ? (
            <div role="alert" className="py-8 text-center text-sm text-foreground">
              <p>{t('violations.timeline.loadError')}</p>
              <button type="button" onClick={() => void refetch()} disabled={isFetching}
                className="mt-3 min-h-9 rounded-md border border-[var(--glass-border)] px-3 py-1.5 hover:bg-[var(--glass-bg)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-400 disabled:opacity-50">
                {t('violations.timeline.retry')}
              </button>
            </div>
          ) : !items.length ? (
            <div className="py-10 text-center text-sm text-muted-foreground">{t('violations.timeline.empty')}</div>
          ) : (
            <div className="pt-2">
              {items.map((e, i) => <EventRow key={`${e.type}-${e.id ?? e.ts}-${i}`} e={e} t={t} />)}
            </div>
          )}
        </div>
        {!isError && pages > 1 && (
          <div className="shrink-0 flex items-center justify-between gap-2 pt-2 border-t border-[var(--glass-border)]">
            <span className="text-xs text-muted-foreground">
              {t('violations.timeline.counter', { page, pages, total })}
            </span>
            <div className="flex items-center gap-1">
              <button type="button" disabled={page <= 1 || isFetching} onClick={() => setPage((p) => Math.max(1, p - 1))}
                className={cn('min-h-8 px-2 py-1 rounded-md text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-400',
                  page <= 1 ? 'text-muted-foreground/40' : 'text-muted-foreground hover:text-white')}>
                {t('common.prev')}
              </button>
              <button type="button" disabled={page >= pages || isFetching} onClick={() => setPage((p) => Math.min(pages, p + 1))}
                className={cn('min-h-8 px-2 py-1 rounded-md text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-400',
                  page >= pages ? 'text-muted-foreground/40' : 'text-muted-foreground hover:text-white')}>
                {t('common.next')}
              </button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
