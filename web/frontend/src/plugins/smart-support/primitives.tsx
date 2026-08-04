/**
 * Shared visual primitives for the smart-support plugin: skeletons,
 * empty states, and the copy-to-clipboard chip. Kept in one file
 * because they're tiny and used everywhere.
 *
 * Visual rules:
 * - All loading states are skeletons, never plain "loading…" text —
 *   skeletons let the operator see *what* is loading without a layout
 *   shift when the content arrives.
 * - All empty states pair an icon with one sentence of guidance — a
 *   bare "no data" line reads as a bug.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Activity,
  Check,
  Copy,
  Inbox,
  Network,
  Server,
  Sparkles,
  Users,
  type LucideIcon,
} from '@/components/brand/icons'

export function Skeleton({ className = '' }: { className?: string }) {
  return (
    <div
      className={
        'animate-pulse rounded bg-[var(--glass-bg)] border border-[var(--glass-border)] ' +
        className
      }
    />
  )
}


export function CardSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="glass-card p-5 space-y-3">
      <Skeleton className="h-4 w-32" />
      <div className="space-y-2">
        {Array.from({ length: rows }).map((_, i) => (
          <Skeleton key={i} className="h-3 w-full" />
        ))}
      </div>
    </div>
  )
}


/**
 * Этапы сборки отчёта. Порядок совпадает с тем, что делает бэкенд:
 * аккаунт → подключения → ноды → корреляции → ИИ. Последний этап самый
 * долгий (вызов модели), поэтому на нём индикатор и останавливается.
 */
const REPORT_STAGES = ['account', 'connections', 'nodes', 'clusters', 'ai'] as const
const STAGE_ICONS = [Users, Activity, Server, Network, Sparkles]
const STAGE_SECONDS = 2

export function ReportProgress() {
  const { t } = useTranslation()
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const id = window.setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => window.clearInterval(id)
  }, [])

  const stage = Math.min(Math.floor(elapsed / STAGE_SECONDS), REPORT_STAGES.length - 1)
  const StageIcon = STAGE_ICONS[stage]
  // Ползём к 90% и там ждём: дорисовывать до 100% раньше ответа — врать.
  const width = Math.min(90, 8 + elapsed * 7)

  return (
    <div className="glass-card p-5 space-y-4" role="status" aria-live="polite">
      <div className="flex items-center gap-3">
        <StageIcon className="w-5 h-5 text-primary-400 shrink-0" aria-hidden />
        <div className="min-w-0">
          <p className="text-sm text-dark-100">
            {t(`plugins.smart_support.report.loading.stages.${REPORT_STAGES[stage]}`)}
          </p>
          {elapsed >= 8 && (
            <p className="text-xs text-dark-400 mt-0.5">
              {t('plugins.smart_support.report.loading.slow_hint')}
            </p>
          )}
        </div>
        <span className="ml-auto text-xs tabular-nums text-dark-400">{elapsed} с</span>
      </div>

      <div className="h-1 rounded-full bg-[var(--glass-bg)] overflow-hidden">
        <div
          className="h-full rounded-full bg-primary-400/70 transition-[width] duration-1000 ease-out"
          style={{ width: `${width}%` }}
        />
      </div>

      <div className="flex gap-1.5" aria-hidden>
        {REPORT_STAGES.map((name, i) => (
          <span
            key={name}
            className={
              'h-1 flex-1 rounded-full transition-colors duration-500 ' +
              (i <= stage ? 'bg-primary-400/50' : 'bg-[var(--glass-bg)]')
            }
          />
        ))}
      </div>
    </div>
  )
}


export function ReportSkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-4 w-40" />
      <ReportProgress />
      <div className="grid gap-6 lg:grid-cols-2">
        <CardSkeleton rows={5} />
        <CardSkeleton rows={5} />
        <CardSkeleton rows={4} />
        <CardSkeleton rows={4} />
      </div>
    </div>
  )
}


export function EmptyState({
  icon: Icon = Inbox,
  message,
  hint,
}: {
  icon?: LucideIcon
  message: string
  hint?: string
}) {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center">
      <div className="rounded-full bg-[var(--glass-bg)] p-3 mb-3">
        <Icon className="w-5 h-5 text-dark-300" aria-hidden />
      </div>
      <p className="text-sm text-dark-200">{message}</p>
      {hint && <p className="mt-1 text-xs text-dark-400 max-w-sm">{hint}</p>}
    </div>
  )
}


/**
 * Кнопка «скопировать» с подписью — для длинного текста, который целиком
 * в чип не влезет (черновик ответа клиенту). CopyChip рядом остаётся для
 * коротких идентификаторов.
 */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Буфер недоступен (http-origin) — молчим, текст можно выделить руками.
    }
  }

  return (
    <button
      type="button"
      onClick={onCopy}
      className={
        'inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded ' +
        'border border-[var(--glass-border)] transition-colors active:scale-95 ' +
        (copied
          ? 'text-emerald-300 border-emerald-400/40'
          : 'text-dark-200 hover:text-white hover:bg-[var(--glass-bg)]')
      }
    >
      {copied ? <Check className="w-3 h-3" aria-hidden /> : <Copy className="w-3 h-3" aria-hidden />}
      {label}
    </button>
  )
}


export function CopyChip({
  value,
  className = '',
}: {
  value: string
  className?: string
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const onCopy = async (e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard blocked (e.g. http origin) — silently no-op rather
      // than throwing a toast at the user; they can still triple-click.
    }
  }

  return (
    <button
      type="button"
      onClick={onCopy}
      title={t('plugins.smart_support.report.copy', { defaultValue: 'Copy' })}
      aria-label={t('plugins.smart_support.report.copy', { defaultValue: 'Copy' })}
      className={
        'inline-flex items-center gap-1 text-[11px] font-mono text-dark-300 ' +
        'hover:text-white hover:bg-[var(--glass-bg)] active:scale-95 ' +
        'rounded px-1.5 py-0.5 transition-all duration-150 ' +
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/40 ' +
        className
      }
    >
      <span className="truncate max-w-[24ch]">{value}</span>
      {copied ? (
        <Check className="w-3 h-3 text-emerald-400 shrink-0" aria-hidden />
      ) : (
        <Copy className="w-3 h-3 shrink-0 opacity-60" aria-hidden />
      )}
    </button>
  )
}


/**
 * Linear progress bar tinted by threshold:
 * - <70%  emerald
 * - 70-89% amber
 * - >=90%  red
 *
 * Used for traffic usage in UserCard. The accessible alternative
 * (the `%` value) is rendered next to it by the caller.
 */
export function ThresholdBar({
  percent,
  className = '',
}: {
  percent: number | null | undefined
  className?: string
}) {
  if (percent == null) return null
  const clamped = Math.max(0, Math.min(100, percent))
  let tint = 'bg-emerald-500'
  if (clamped >= 90) tint = 'bg-red-500'
  else if (clamped >= 70) tint = 'bg-amber-500'
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(clamped)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={
        'h-1.5 w-full rounded-full bg-[var(--glass-bg)] overflow-hidden ' + className
      }
    >
      <div
        className={`h-full ${tint} transition-all duration-500`}
        style={{ width: `${clamped}%` }}
      />
    </div>
  )
}
