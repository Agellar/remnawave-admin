import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, CheckCircle, ChevronDown, Clock, Globe2, History, Loader2, Network, RefreshCw, Server, Settings, ShieldBan, Sparkles, XCircle, Zap } from '@/components/brand/icons'
import { toast } from 'sonner'

import LicenseBanner from '@/components/plugins/license'
import { Button } from '@/components/ui/button'

import DataList from './DataList'

import { analyzeAlert, asLicenseError, fetchAlerts, fetchHosters, fetchOverview, fetchProbes, fetchStatus, runProbeNow } from './api'
import type {
  RadarAlert,
  RadarNodeDip,
  RadarOverview,
  RadarProbes,
  RadarProbeState,
  RadarSite,
  RadarStatus,
  RadarTick,
} from './types'

const TRANSPORT_LABELS: Record<string, string> = {
  reality: 'Reality',
  ws: 'WebSocket',
  xhttp: 'XHTTP',
  tls: 'TLS',
  grpc: 'gRPC',
  trojan: 'Trojan',
  ss: 'Shadowsocks',
}

function transportLabel(t: (k: string) => string, transport: string): string {
  if (transport === 'mixed') return t('plugins.block_radar.transport_mixed')
  if (transport === 'other') return t('plugins.block_radar.transport_other')
  return TRANSPORT_LABELS[transport] ?? transport
}

function asnLabel(org: string | null | undefined, asn: number): string {
  // Negative ids are stable local identifiers used by a self-hosted radar
  // when no public ASN enrichment is available. Do not present them as ASNs.
  if (asn <= 0) return org || 'Local'
  return org ? `${org} (AS${asn})` : `AS${asn}`
}

/** Оператор алерта: у агрегата по хостеру его нет, там нулевой ASN. */
function opLabel(t: (k: string) => string, alert: RadarAlert): string {
  if (alert.scope === 'hoster' || !alert.op_asn) {
    return t('plugins.block_radar.all_operators')
  }
  return asnLabel(alert.op_org, alert.op_asn)
}

/**
 * /plugins/block-radar — сеть панелей и её инциденты.
 *
 * Данные локальные (таблица плагина + статус последнего тика), поэтому
 * страница дешёвая и обновляется каждые 30 секунд. 402 от бэка — плагин
 * куплен, но подписка неактивна — показываем общий баннер лицензии.
 */
export default function RadarPage() {
  const { t } = useTranslation()

  const status = useQuery({
    queryKey: ['block-radar-status'],
    queryFn: fetchStatus,
    retry: false,
    refetchInterval: 30_000,
  })
  const open = useQuery({
    queryKey: ['block-radar-alerts-open'],
    queryFn: () => fetchAlerts({ active: true, limit: 100 }),
    retry: false,
    refetchInterval: 30_000,
  })
  const history = useQuery({
    queryKey: ['block-radar-alerts-history'],
    queryFn: () => fetchAlerts({ active: false, limit: 25 }),
    retry: false,
    refetchInterval: 60_000,
  })

  const overview = useQuery({
    queryKey: ['block-radar-overview'],
    queryFn: fetchOverview,
    retry: false,
    refetchInterval: 120_000,
  })
  const probes = useQuery({
    queryKey: ['block-radar-probes'],
    queryFn: fetchProbes,
    retry: false,
    refetchInterval: 30_000,
  })

  const licenseError = useMemo(
    () => (status.error ? asLicenseError(status.error) : null),
    [status.error],
  )

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Activity className="w-6 h-6 text-emerald-400 shrink-0" aria-hidden />
          <div className="min-w-0">
            <h1 className="text-xl sm:text-2xl font-bold text-white truncate">
              {t('plugins.block_radar.title')}
            </h1>
            <p className="text-sm text-dark-300">{t('plugins.block_radar.subtitle')}</p>
          </div>
        </div>
        <Link
          to="/plugins/block-radar/settings"
          className="inline-flex items-center gap-2 text-sm text-dark-300 hover:text-white transition-colors shrink-0"
        >
          <Settings className="w-4 h-4" aria-hidden />
          {t('plugins.block_radar.settings_link')}
        </Link>
      </div>

      {licenseError && <LicenseBanner error={licenseError} />}

      {!licenseError && <ExpiryNotice status={status.data ?? null} />}

      {!licenseError && <OverviewCards data={overview.data ?? null} />}

      {!licenseError && <StatusCard tick={status.data?.last_tick ?? null} />}

      {!licenseError && (
        <ReachabilityPanel
          data={probes.data ?? null}
          loading={probes.isLoading}
          error={probes.isError}
        />
      )}

      {!licenseError && (overview.data?.sites.length ?? 0) > 0 && (
        <SitesTable sites={overview.data!.sites} />
      )}

      {!licenseError && overview.data && <NetworkPulse pulse={overview.data.pulse} />}

      {(status.data?.open_dips?.length ?? 0) > 0 && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-white uppercase tracking-wider flex items-center gap-2">
            <ShieldBan className="w-4 h-4 text-amber-400" aria-hidden />
            {t('plugins.block_radar.dips_title')}
          </h2>
          <div className="grid gap-3 lg:grid-cols-2">
            {(status.data?.open_dips ?? []).map((dip) => (
              <NodeDipCard key={dip.node_uuid} dip={dip} />
            ))}
          </div>
        </section>
      )}

      {!licenseError && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-white uppercase tracking-wider flex items-center gap-2">
            <ShieldBan className="w-4 h-4 text-red-400" aria-hidden />
            {t('plugins.block_radar.open_title')}
            {open.data && open.data.total > 0 && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/15 text-red-300">
                {open.data.total}
              </span>
            )}
          </h2>
          {open.data && open.data.items.length === 0 && (
            <div className="glass-card p-5 flex items-center gap-3">
              <CheckCircle className="w-5 h-5 text-emerald-400 shrink-0" aria-hidden />
              <p className="text-sm text-dark-200">{t('plugins.block_radar.all_quiet')}</p>
            </div>
          )}
          <div className="grid gap-3 lg:grid-cols-2">
            {(open.data?.items ?? []).map((a) => (
              <AlertCard key={a.id} alert={a} />
            ))}
          </div>
        </section>
      )}

      {!licenseError && (history.data?.items.length ?? 0) > 0 && (
        <section className="radar-deferred space-y-3">
          <h2 className="text-sm font-semibold text-white uppercase tracking-wider">
            {t('plugins.block_radar.history_title')}
          </h2>
          <DataList
            rows={history.data!.items}
            rowKey={(a) => a.id}
            columns={[
              {
                title: t('plugins.block_radar.col_link'),
                primary: true,
                cell: (a) => (
                  <>
                    {opLabel(t, a)} → {asnLabel(a.host_org, a.host_asn)} ·{' '}
                    {transportLabel(t, a.transport)}
                  </>
                ),
              },
              {
                title: t('plugins.block_radar.col_kind'),
                cell: (a) =>
                  t(
                    a.kind === 'operator_outage'
                      ? 'plugins.block_radar.kind_outage'
                      : 'plugins.block_radar.kind_block',
                  ),
              },
              {
                title: t('plugins.block_radar.col_since'),
                nowrap: true,
                cell: (a) => formatTs(a.since),
              },
              {
                title: t('plugins.block_radar.col_resolved'),
                nowrap: true,
                cell: (a) => (a.resolved_at ? formatTs(a.resolved_at) : '—'),
              },
            ]}
          />
        </section>
      )}

      {!licenseError && <HosterRating />}
    </div>
  )
}


/**
 * Рейтинг хостеров по данным всей сети панелей.
 *
 * Показываем только тех, кого достаточно долго видят несколько независимых
 * панелей. Пока сеть копится, таблица пуста — и тогда важнее показать, что
 * данные идут, чем нарисовать рейтинг из одного наблюдателя.
 */
function HosterRating() {
  const { t } = useTranslation()

  const rating = useQuery({
    queryKey: ['block-radar-hosters'],
    queryFn: fetchHosters,
    retry: false,
    refetchInterval: 300_000,
  })

  if (rating.isError || rating.data?.locked) return null

  const data = rating.data
  const items = data?.hosters ?? []

  return (
    <section className="radar-deferred space-y-3">
      <h2 className="text-sm font-semibold text-white uppercase tracking-wider flex items-center gap-2">
        <Server className="w-4 h-4 text-sky-400" aria-hidden />
        {t('plugins.block_radar.hosters_title')}
      </h2>

      {items.length === 0 ? (
        <div className="glass-card p-5">
          <p className="text-sm text-dark-300">
            {t('plugins.block_radar.hosters_pending', {
              count: data?.pending ?? 0,
              panels: data?.min_panels ?? 3,
            })}
          </p>
        </div>
      ) : (
        <DataList
          rows={items}
          rowKey={(h) => h.asn}
          columns={[
            {
              title: t('plugins.block_radar.col_hoster'),
              primary: true,
              cell: (h) => (
                <>
                  {asnLabel(h.org, h.asn)}
                  {h.mine && (
                    <span className="ml-2 text-[10px] uppercase tracking-wider text-emerald-400">
                      {t('plugins.block_radar.hoster_mine')}
                    </span>
                  )}
                </>
              ),
            },
            { title: t('plugins.block_radar.col_panels'), nowrap: true, cell: (h) => h.panels },
            {
              title: t('plugins.block_radar.col_outages'),
              nowrap: true,
              cell: (h) => (
                <>
                  {h.outages_per_month.toFixed(1)}
                  <span className="text-dark-400 text-xs"> /мес</span>
                </>
              ),
            },
            {
              title: t('plugins.block_radar.col_blocks'),
              nowrap: true,
              cell: (h) => (
                <>
                  {h.blocks_per_month.toFixed(1)}
                  <span className="text-dark-400 text-xs"> /мес</span>
                </>
              ),
            },
            {
              title: t('plugins.block_radar.col_recovery'),
              nowrap: true,
              cell: (h) =>
                h.median_recovery_minutes != null
                  ? t('plugins.block_radar.minutes', { count: h.median_recovery_minutes })
                  : '—',
            },
          ]}
        />
      )}
      {items.length > 0 && (
        <p className="px-1 text-xs text-dark-400">
          {t('plugins.block_radar.hosters_note', {
            days: data?.window_days ?? 30,
            panels: data?.min_panels ?? 3,
          })}
        </p>
      )}
    </section>
  )
}


const PROBE_STATE_CLASS: Record<RadarProbeState, string> = {
  healthy: 'bg-emerald-500/15 text-emerald-200',
  degraded: 'bg-amber-500/15 text-amber-200',
  regional_suspect: 'bg-orange-500/15 text-orange-200',
  endpoint_down: 'bg-red-500/15 text-red-200',
  insufficient: 'bg-white/5 text-dark-300',
}

const PROBE_STATE_DOT: Record<RadarProbeState, string> = {
  healthy: 'bg-emerald-400',
  degraded: 'bg-amber-400',
  regional_suspect: 'bg-orange-400',
  endpoint_down: 'bg-red-400',
  insufficient: 'bg-dark-400',
}

const PROBE_STATE_PRIORITY: RadarProbeState[] = [
  'endpoint_down',
  'regional_suspect',
  'degraded',
  'insufficient',
  'healthy',
]

function ReachabilityPanel({
  data,
  loading,
  error,
}: {
  data: RadarProbes | null
  loading: boolean
  error: boolean
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const runNow = useMutation({
    mutationFn: runProbeNow,
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['block-radar-probes'] }),
        qc.invalidateQueries({ queryKey: ['block-radar-status'] }),
      ])
      toast.success(t('plugins.block_radar.reachability.manual_done'))
    },
    onError: () => toast.error(t('plugins.block_radar.reachability.manual_error')),
  })
  const running = runNow.isPending || Boolean(data?.probe_running)
  const manualDisabled = running || !data?.configured || !data?.enabled
  const targets = data?.items ?? []
  const overallState = PROBE_STATE_PRIORITY.find((state) =>
    targets.some((target) => target.state === state),
  ) ?? 'insufficient'
  const healthyTargets = targets.filter((target) => target.state === 'healthy').length
  const lastProbeAt = data?.last_probe_at
    ?? targets.reduce<string | null>((latest, target) => (
      !latest || new Date(target.sampled_at) > new Date(latest) ? target.sampled_at : latest
    ), null)

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-x-5 gap-y-3">
        <div className="flex items-start gap-2">
          <Globe2 className="mt-0.5 h-4 w-4 shrink-0 text-cyan-300" aria-hidden />
          <div>
            <h2 className="text-sm font-semibold text-white">
              {t('plugins.block_radar.reachability.title')}
            </h2>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-dark-300">
              {t('plugins.block_radar.reachability.subtitle')}
            </p>
          </div>
        </div>
        {data && (
          <span className={`inline-flex min-h-7 items-center rounded-md px-2.5 py-1 text-xs font-medium ${
            data.configured ? 'bg-emerald-500/15 text-emerald-200' : 'bg-amber-500/15 text-amber-200'
          }`}>
            {t(data.configured
              ? 'plugins.block_radar.reachability.configured'
              : 'plugins.block_radar.reachability.not_configured')}
          </span>
        )}
      </div>

      <div className="glass-card overflow-visible">
        {loading && (
          <div className="space-y-4 p-5 sm:p-6">
            <div className="h-5 w-56 animate-pulse rounded bg-white/10" />
            <div className="h-24 animate-pulse rounded-xl bg-white/5" />
          </div>
        )}
        {!loading && error && (
          <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between sm:p-6">
            <div className="flex items-center gap-3">
              <XCircle className="h-5 w-5 shrink-0 text-red-300" aria-hidden />
              <p className="text-sm text-dark-200">
                {t('plugins.block_radar.reachability.load_error')}
              </p>
            </div>
            <Button
              variant="outline"
              className="h-11 w-full sm:w-auto"
              onClick={() => qc.invalidateQueries({ queryKey: ['block-radar-probes'] })}
            >
              <RefreshCw className="mr-2 h-4 w-4" aria-hidden />
              {t('plugins.block_radar.reachability.retry')}
            </Button>
          </div>
        )}
        {!loading && !error && targets.length === 0 && (
          <div className="flex items-center gap-3 p-5 sm:p-6">
            <Network className="h-5 w-5 shrink-0 text-dark-400" aria-hidden />
            <p className="text-sm text-dark-300">
              {t('plugins.block_radar.reachability.empty')}
            </p>
          </div>
        )}
        {!loading && !error && targets.length > 0 && (
          <>
            <div className="p-5 sm:p-6">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${PROBE_STATE_DOT[overallState]}`} />
                    <p className="text-base font-semibold text-white sm:text-lg">
                      {t(`plugins.block_radar.reachability.summary.${overallState}`)}
                    </p>
                  </div>
                  <p className="mt-1 text-sm text-dark-300">
                    {t('plugins.block_radar.reachability.available_targets', {
                      healthy: healthyTargets,
                      total: targets.length,
                    })}
                  </p>
                </div>
                <Button
                  variant="outline"
                  disabled={manualDisabled}
                  className="h-11 w-full px-4 sm:w-auto"
                  onClick={() => runNow.mutate()}
                >
                  <RefreshCw className={`mr-2 h-4 w-4 ${running ? 'animate-spin' : ''}`} aria-hidden />
                  {t(running
                    ? 'plugins.block_radar.reachability.manual_running'
                    : 'plugins.block_radar.reachability.manual_run')}
                </Button>
              </div>

              <div className="relative mt-6 grid gap-5 md:grid-cols-3 md:gap-4">
                <div className="absolute bottom-5 left-[17px] top-5 w-px bg-white/10 md:bottom-auto md:left-[16.667%] md:right-[16.667%] md:top-[17px] md:h-px md:w-auto" />
                <ScheduleStep
                  icon={<History className="h-4 w-4" aria-hidden />}
                  label={t('plugins.block_radar.reachability.last_check')}
                  value={lastProbeAt ? formatTs(lastProbeAt) : t('plugins.block_radar.reachability.never')}
                />
                <ScheduleStep
                  icon={running
                    ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                    : <Activity className="h-4 w-4" aria-hidden />}
                  label={t('plugins.block_radar.reachability.now')}
                  value={t(running
                    ? 'plugins.block_radar.reachability.timer_running'
                    : `plugins.block_radar.reachability.states.${overallState}`)}
                  accent
                />
                <ScheduleStep
                  icon={<Clock className="h-4 w-4" aria-hidden />}
                  label={t('plugins.block_radar.reachability.next_check')}
                  value={(
                    <ProbeCountdown
                      enabled={Boolean(data?.enabled)}
                      running={running}
                      nextProbeAt={data?.next_probe_at}
                    />
                  )}
                />
              </div>
            </div>

            <div className="border-t border-white/[0.07]">
              <div className="px-5 pb-2 pt-5 sm:px-6">
                <h3 className="text-sm font-semibold text-white">
                  {t('plugins.block_radar.reachability.targets_title')}
                </h3>
                <p className="mt-1 text-xs text-dark-400">
                  {t('plugins.block_radar.reachability.targets_help')}
                </p>
              </div>
              {targets.map((target) => (
                <ProbeTargetRow key={target.target_uuid} target={target} />
              ))}
            </div>

            <details className="group border-t border-white/[0.07] px-5 py-4 sm:px-6">
              <summary className="flex min-h-8 cursor-pointer list-none items-center justify-between gap-3 rounded-md text-xs text-dark-400 hover:text-dark-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60">
                <span>{t('plugins.block_radar.reachability.privacy_summary')}</span>
                <ChevronDown className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180" aria-hidden />
              </summary>
              <p className="mt-3 max-w-3xl text-xs leading-relaxed text-dark-400">
                {t('plugins.block_radar.reachability.privacy')}
              </p>
            </details>
          </>
        )}
      </div>
    </section>
  )
}

function ScheduleStep({
  icon,
  label,
  value,
  accent = false,
}: {
  icon: ReactNode
  label: string
  value: ReactNode
  accent?: boolean
}) {
  return (
    <div className="relative z-10 flex min-w-0 items-start gap-3 md:block md:text-center">
      <span className={`inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full border ${
        accent
          ? 'border-cyan-400/40 bg-cyan-400/15 text-cyan-200'
          : 'border-white/10 bg-dark-900 text-dark-300'
      }`}>
        {icon}
      </span>
      <div className="min-w-0 pt-0.5 md:mt-2 md:pt-0">
        <div className="text-xs text-dark-400">{label}</div>
        <div className="mt-0.5 truncate text-sm font-medium tabular-nums text-dark-100">{value}</div>
      </div>
    </div>
  )
}

function ProbeTargetRow({ target }: { target: RadarProbes['items'][number] }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(target.state !== 'healthy')
  const ruResults = target.results.filter((result) => result.country === 'RU')
  const controlResults = target.results.filter((result) => result.country !== 'RU')

  useEffect(() => {
    if (target.state !== 'healthy') setOpen(true)
  }, [target.state])

  return (
    <details
      className="group border-b border-white/[0.06] px-5 py-4 last:border-b-0 sm:px-6"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer list-none rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.35fr)_minmax(15rem,.9fr)_auto] lg:items-center">
          <div className="flex min-w-0 items-start gap-3">
            <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${PROBE_STATE_DOT[target.state]}`} />
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-white" title={target.target_name}>
                {target.target_name}
              </div>
              <div className="mt-1 text-xs text-dark-400">
                {t('plugins.block_radar.reachability.port', { port: target.target_port })} · {formatTs(target.sampled_at)}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <ProbeRouteSummary
              label={t('plugins.block_radar.reachability.ru')}
              value={`${target.ru_success}/${target.ru_total}`}
              healthy={target.ru_total > 0 && target.ru_success >= Math.ceil(target.ru_total / 2)}
            />
            <ProbeRouteSummary
              label={t('plugins.block_radar.reachability.controls')}
              value={`${target.control_success}/${target.control_total}`}
              healthy={target.control_total > 0 && target.control_success > 0}
            />
          </div>

          <div className="flex items-center justify-between gap-3 lg:justify-end">
            <span className={`rounded-md px-2.5 py-1.5 text-xs font-medium ${PROBE_STATE_CLASS[target.state]}`}>
              {t(`plugins.block_radar.reachability.states.${target.state}`)}
            </span>
            <ChevronDown className="h-4 w-4 shrink-0 text-dark-400 transition-transform group-open:rotate-180" aria-hidden />
          </div>
        </div>
      </summary>

      <div className="mt-4 grid gap-4 border-t border-white/[0.06] pt-4 lg:grid-cols-2">
        <ProbeResultGroup
          label={t('plugins.block_radar.reachability.ru_route')}
          results={ruResults}
        />
        <ProbeResultGroup
          label={t('plugins.block_radar.reachability.control_route')}
          results={controlResults}
        />
      </div>
    </details>
  )
}

function ProbeRouteSummary({
  label,
  value,
  healthy,
}: {
  label: string
  value: string
  healthy: boolean
}) {
  return (
    <div className="flex items-center gap-2">
      <span className={`h-2 w-2 shrink-0 rounded-full ${healthy ? 'bg-emerald-400' : 'bg-red-400'}`} />
      <div className="min-w-0">
        <div className="truncate text-[11px] text-dark-400">{label}</div>
        <div className="text-sm font-medium tabular-nums text-dark-100">{value}</div>
      </div>
    </div>
  )
}

function ProbeResultGroup({
  label,
  results,
}: {
  label: string
  results: RadarProbes['items'][number]['results']
}) {
  const { t } = useTranslation()
  return (
    <div>
      <div className="text-xs font-medium text-dark-300">{label}</div>
      <div className="mt-2 space-y-1.5">
        {results.length === 0 && (
          <p className="text-xs text-dark-500">{t('plugins.block_radar.reachability.no_results')}</p>
        )}
        {results.map((result, index) => (
          <div
            key={`${result.source}-${result.vantage_label}-${index}`}
            className="flex min-h-8 items-center justify-between gap-3 rounded-lg bg-white/[0.035] px-2.5 py-1.5"
          >
            <div className="flex min-w-0 items-center gap-2">
              {result.success
                ? <CheckCircle className="h-3.5 w-3.5 shrink-0 text-emerald-300" aria-hidden />
                : <XCircle className="h-3.5 w-3.5 shrink-0 text-red-300" aria-hidden />}
              <span className="truncate text-xs text-dark-200" title={result.vantage_label}>
                {result.vantage_label}
              </span>
            </div>
            <span className={`shrink-0 text-xs tabular-nums ${result.success ? 'text-emerald-200' : 'text-red-200'}`}>
              {result.latency_ms != null
                ? `${Math.round(result.latency_ms)} ms`
                : result.error_code ?? t('plugins.block_radar.reachability.failed')}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ProbeCountdown({
  enabled,
  running,
  nextProbeAt,
}: {
  enabled: boolean
  running: boolean
  nextProbeAt?: string | null
}) {
  const { t } = useTranslation()
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!enabled || running || !nextProbeAt) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [enabled, running, nextProbeAt])

  let value = t('plugins.block_radar.reachability.timer_paused')
  if (running) {
    value = t('plugins.block_radar.reachability.timer_running')
  } else if (enabled && nextProbeAt) {
    const target = new Date(nextProbeAt).getTime()
    const seconds = Number.isFinite(target) ? Math.max(0, Math.ceil((target - now) / 1_000)) : 0
    const minutes = Math.floor(seconds / 60)
    value = t('plugins.block_radar.reachability.timer_next', {
      value: `${minutes}:${String(seconds % 60).padStart(2, '0')}`,
    })
  }

  return (
    <span aria-live="off">{value}</span>
  )
}

function StatusCard({ tick }: { tick: RadarTick | null }) {
  const { t } = useTranslation()

  if (!tick) {
    return (
      <div className="glass-card p-5">
        <p className="text-sm text-dark-300">{t('plugins.block_radar.status.never')}</p>
      </div>
    )
  }

  const items: Array<{ label: string; value: string }> = [
    { label: t('plugins.block_radar.status.last_tick'), value: formatTs(tick.at) },
    { label: t('plugins.block_radar.status.cells'), value: String(tick.cells ?? 0) },
    {
      label: t('plugins.block_radar.status.links'),
      value: `${tick.links_active ?? 0} / ${tick.links_zero ?? 0}`,
    },
    {
      label: t('plugins.block_radar.status.nodes'),
      value: `${(tick.nodes_total ?? 0) - (tick.nodes_skipped ?? 0)} / ${tick.nodes_total ?? 0}`,
    },
  ]

  return (
    <div className="glass-card p-5 space-y-3">
      <div className="flex items-center gap-2">
        <Zap className="w-4 h-4 text-cyan-400" aria-hidden />
        <h2 className="text-sm font-semibold text-white uppercase tracking-wider">
          {t('plugins.block_radar.status.title')}
        </h2>
        <span
          className={`ml-auto text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
            tick.ok ? 'bg-emerald-500/15 text-emerald-300' : 'bg-red-500/15 text-red-300'
          }`}
        >
          {tick.ok
            ? t('plugins.block_radar.status.ok')
            : t(`plugins.block_radar.status.errors.${tick.error ?? 'exception'}`, {
                defaultValue: tick.error ?? 'error',
              })}
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {items.map((it) => (
          <div key={it.label}>
            <div className="text-[11px] uppercase tracking-wider text-dark-400">{it.label}</div>
            <div className="text-white font-mono text-sm mt-0.5">{it.value}</div>
          </div>
        ))}
      </div>
      {tick.alerts_locked && (
        <p className="text-xs text-amber-300">{t('plugins.block_radar.status.alerts_locked')}</p>
      )}
    </div>
  )
}


function AlertCard({ alert }: { alert: RadarAlert }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const isOutage = alert.kind === 'operator_outage'
  const analysis = alert.ai_analysis
  const mutation = useMutation({
    mutationFn: () => analyzeAlert(alert.id, Boolean(analysis)),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['block-radar-alerts-open'] })
      void qc.invalidateQueries({ queryKey: ['block-radar-alerts-history'] })
      toast.success(t('plugins.block_radar.ai.ready'))
    },
    onError: () => toast.error(t('plugins.block_radar.ai.error')),
  })

  return (
    <div className="glass-card p-4 space-y-2">
      <div className="flex items-center gap-2">
        <span
          className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
            isOutage ? 'bg-amber-500/15 text-amber-300' : 'bg-red-500/15 text-red-300'
          }`}
        >
          {t(isOutage ? 'plugins.block_radar.kind_outage' : 'plugins.block_radar.kind_block')}
        </span>
        <span className="text-xs text-dark-400 ml-auto whitespace-nowrap">
          {formatTs(alert.since)}
        </span>
      </div>
      <div className="text-sm text-white font-medium">
        {transportLabel(t, alert.transport)} · {opLabel(t, alert)}
      </div>
      <div className="text-xs text-dark-300">
        {t('plugins.block_radar.card_host')}: {asnLabel(alert.host_org, alert.host_asn)}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-dark-300">
        {alert.online != null && alert.baseline != null && (
          <span>
            {t('plugins.block_radar.card_online')}:{' '}
            <span className="text-white font-mono">
              {alert.online} / {Math.round(alert.baseline)}
            </span>
          </span>
        )}
        {alert.panels != null && (
          <span>
            {t('plugins.block_radar.card_panels')}:{' '}
            <span className="text-white font-mono">{alert.panels}</span>
          </span>
        )}
      </div>
      {isOutage && alert.outage_summary && (
        <p className="text-xs text-dark-400">{alert.outage_summary}</p>
      )}
      {(alert.affected.nodes?.length ?? 0) > 0 && (
        <p className="text-xs text-dark-300">
          {t('plugins.block_radar.card_affected')}:{' '}
          <span className="text-white">{alert.affected.nodes!.join(', ')}</span>
        </p>
      )}
      <div className="pt-2 border-t border-white/5 space-y-3">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-violet-400" aria-hidden />
          <span className="text-xs font-medium text-white">
            {t('plugins.block_radar.ai.title')}
          </span>
          {analysis && (
            <span className="text-[10px] text-violet-300 ml-auto">
              {t(`plugins.block_radar.ai.classifications.${analysis.classification}`)} ·{' '}
              {Math.round(analysis.confidence * 100)}%
            </span>
          )}
        </div>
        {analysis ? (
          <div className="space-y-2 text-xs">
            <p className="text-dark-200 leading-relaxed">{analysis.summary}</p>
            {analysis.evidence.length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-dark-400 mb-1">
                  {t('plugins.block_radar.ai.evidence')}
                </div>
                <ul className="space-y-1 text-dark-300 list-disc pl-4">
                  {analysis.evidence.map((item) => <li key={item}>{item}</li>)}
                </ul>
              </div>
            )}
            {analysis.recommendations.length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-dark-400 mb-1">
                  {t('plugins.block_radar.ai.recommendations')}
                </div>
                <ul className="space-y-1 text-dark-300 list-disc pl-4">
                  {analysis.recommendations.map((item) => <li key={item}>{item}</li>)}
                </ul>
              </div>
            )}
            {analysis.support_note && (
              <div className="rounded-lg bg-violet-500/10 p-2 text-violet-100">
                <span className="text-violet-300">{t('plugins.block_radar.ai.support_note')}: </span>
                {analysis.support_note}
              </div>
            )}
            <div className="text-[10px] text-dark-500">{analysis.model}</div>
          </div>
        ) : (
          <p className="text-xs text-dark-400">{t('plugins.block_radar.ai.empty')}</p>
        )}
        <Button
          size="sm"
          variant="outline"
          disabled={mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending ? (
            <Loader2 className="w-3.5 h-3.5 mr-2 animate-spin" aria-hidden />
          ) : (
            <Sparkles className="w-3.5 h-3.5 mr-2" aria-hidden />
          )}
          {t(analysis ? 'plugins.block_radar.ai.refresh' : 'plugins.block_radar.ai.analyze')}
        </Button>
      </div>
    </div>
  )
}


/**
 * Плашка «подписка на исходе». Загорается за неделю до конца, чтобы
 * владелец успел продлить — иначе радар просто однажды замолкает.
 */
function ExpiryNotice({ status }: { status: RadarStatus | null }) {
  const { t } = useTranslation()
  if (!status?.license_paid_until) return null

  const days = Math.floor((status.license_paid_until * 1000 - Date.now()) / 86_400_000)
  if (days > 7) return null

  const trial = status.license_tier === 'trial'
  const urgent = days <= 1
  const key =
    days > 1 ? 'expiry_days' : days === 1 ? 'expiry_tomorrow' : days === 0 ? 'expiry_today' : 'expiry_over'

  return (
    <div className="glass-card p-4 flex items-start gap-3">
      <Zap className={`w-5 h-5 shrink-0 ${urgent ? 'text-red-400' : 'text-amber-400'}`} aria-hidden />
      <div className="space-y-1">
        <p className="text-sm text-white font-medium">
          {t(`plugins.block_radar.${key}`, {
            days,
            what: t(trial ? 'plugins.block_radar.expiry_trial' : 'plugins.block_radar.expiry_sub'),
          })}
        </p>
        <p className="text-xs text-dark-300">{t('plugins.block_radar.expiry_hint')}</p>
      </div>
    </div>
  )
}


function NodeDipCard({ dip }: { dip: RadarNodeDip }) {
  const { t } = useTranslation()
  const pct = (v: number) => `${(v * 100).toFixed(1)}%`

  return (
    <div className="glass-card p-4 space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300">
          {t('plugins.block_radar.dip_badge')}
        </span>
        <span className="text-xs text-dark-400 ml-auto whitespace-nowrap">
          {formatTs(dip.since)}
        </span>
      </div>
      <div className="text-sm text-white font-medium">
        {dip.node_name || dip.node_uuid.slice(0, 8)}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-dark-300">
        <span>
          {t('plugins.block_radar.card_online')}:{' '}
          <span className="text-white font-mono">
            {dip.online} / {Math.round(dip.baseline_online)}
          </span>
        </span>
        <span>
          {t('plugins.block_radar.dip_share')}:{' '}
          <span className="text-white font-mono">
            {pct(dip.share)} / {pct(dip.baseline_share)}
          </span>
        </span>
      </div>
      <p className="text-xs text-dark-300">
        {t(dip.node_alive ? 'plugins.block_radar.dip_alive' : 'plugins.block_radar.dip_offline')}
      </p>
    </div>
  )
}


function formatTs(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString()
}

/** Одна цифра сводки: значение, короткое имя и пояснение под ним. */
function StatCard({
  value,
  label,
  hint,
  extra,
}: {
  value: string
  label: string
  hint: string
  extra?: string | null
}) {
  return (
    <div className="glass-card p-4 flex flex-col gap-1">
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-bold text-white tabular-nums">{value}</span>
        {extra && <span className="text-xs text-emerald-300">{extra}</span>}
      </div>
      <div className="text-xs uppercase tracking-wider text-dark-300">{label}</div>
      {/* Без пояснения «маршруты» и «сети абонентов» — внутренний жаргон. */}
      <div className="text-[11px] text-dark-400 leading-snug">{hint}</div>
    </div>
  )
}

function OverviewCards({ data }: { data: RadarOverview | null }) {
  const { t } = useTranslation()
  if (!data) return null

  const armed = data.links.armed
  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <StatCard
        value={String(data.links.measured)}
        extra={armed > 0 ? t('plugins.block_radar.stat_armed', { count: armed }) : null}
        label={t('plugins.block_radar.stat_links')}
        hint={t('plugins.block_radar.stat_links_hint')}
      />
      <StatCard
        value={String(data.operators)}
        label={t('plugins.block_radar.stat_operators')}
        hint={t('plugins.block_radar.stat_operators_hint')}
      />
      <StatCard
        value={String(data.hosters)}
        label={t('plugins.block_radar.stat_sites')}
        hint={t('plugins.block_radar.stat_sites_hint')}
      />
      <StatCard
        value={String(data.network.panels)}
        label={t('plugins.block_radar.stat_network')}
        hint={t('plugins.block_radar.stat_network_hint', {
          hosters: data.network.hosters,
        })}
      />
    </section>
  )
}

function SitesTable({ sites }: { sites: RadarSite[] }) {
  const { t } = useTranslation()
  return (
    <section className="radar-deferred space-y-3">
      <h2 className="text-sm font-semibold text-white uppercase tracking-wider flex items-center gap-2">
        <Server className="w-4 h-4 text-emerald-400" aria-hidden />
        {t('plugins.block_radar.sites_title')}
      </h2>
      <DataList
        rows={sites}
        rowKey={(s) => `${s.host_asn}-${s.transport}`}
        columns={[
          {
            title: t('plugins.block_radar.col_hoster'),
            primary: true,
            cell: (s) => asnLabel(s.host_org, s.host_asn),
          },
          {
            title: t('plugins.block_radar.col_transport'),
            cell: (s) => transportLabel(t, s.transport),
          },
          {
            title: t('plugins.block_radar.col_online'),
            nowrap: true,
            cell: (s) => s.online ?? '—',
          },
          {
            title: t('plugins.block_radar.col_norm'),
            nowrap: true,
            cell: (s) =>
              s.baseline != null ? (
                Math.round(s.baseline)
              ) : (
                <span className="text-dark-400">{t('plugins.block_radar.norm_pending')}</span>
              ),
          },
          {
            title: t('plugins.block_radar.col_watchers'),
            nowrap: true,
            cell: (s) =>
              s.panels > 1 ? (
                s.panels
              ) : (
                <span className="text-dark-400">{t('plugins.block_radar.only_you')}</span>
              ),
          },
        ]}
      />
    </section>
  )
}

/**
 * Пульс сети — то, за что, собственно, платят: чужой опыт по площадкам,
 * которых у владельца может и не быть. Только агрегаты по ASN, без указания
 * чьи панели видели провал.
 */
function NetworkPulse({ pulse }: { pulse: RadarOverview['pulse'] }) {
  const { t } = useTranslation()
  const top = pulse.hosters_top

  return (
    <section className="radar-deferred space-y-3">
      <h2 className="text-sm font-semibold text-white uppercase tracking-wider flex items-center gap-2">
        <Zap className="w-4 h-4 text-emerald-400" aria-hidden />
        {t('plugins.block_radar.pulse_title', { days: pulse.days })}
      </h2>

      {pulse.incidents === 0 ? (
        <div className="glass-card p-5 flex items-center gap-3">
          <CheckCircle className="w-5 h-5 text-emerald-400 shrink-0" aria-hidden />
          <p className="text-sm text-dark-200">{t('plugins.block_radar.pulse_calm')}</p>
        </div>
      ) : (
        <>
          <p className="text-sm text-dark-300">
            {t('plugins.block_radar.pulse_summary', {
              blocks: pulse.blocks,
              outages: pulse.outages,
              hosters: pulse.hosters,
            })}
          </p>
          <DataList
            rows={top}
            rowKey={(h) => h.host_asn}
            columns={[
              {
                title: t('plugins.block_radar.col_hoster'),
                primary: true,
                cell: (h) => (
                  <>
                    {asnLabel(h.host_org, h.host_asn)}
                    {h.is_mine && (
                      <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300">
                        {t('plugins.block_radar.hoster_mine')}
                      </span>
                    )}
                  </>
                ),
              },
              { title: t('plugins.block_radar.col_blocks'), nowrap: true, cell: (h) => h.blocks },
              { title: t('plugins.block_radar.col_outages'), nowrap: true, cell: (h) => h.outages },
              {
                title: t('plugins.block_radar.col_last'),
                nowrap: true,
                cell: (h) => (h.last_at ? formatTs(h.last_at) : '—'),
              },
            ]}
          />
          <p className="text-[11px] text-dark-400">{t('plugins.block_radar.pulse_note')}</p>
        </>
      )}
    </section>
  )
}
