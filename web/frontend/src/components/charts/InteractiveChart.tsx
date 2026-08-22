/**
 * InteractiveChart — переиспользуемый график в стиле панели с «графана-фишками»:
 * переключение вида (область/линия/столбцы), zoom-brush, экспорт CSV,
 * мультисерии, единая тема (useChartTheme).
 *
 * Период/диапазон дат остаётся за вызывающим (данные приходят готовыми) —
 * компонент отвечает за визуализацию и интерактив над данными.
 */
import { useMemo, useState, useRef, useEffect, ReactElement } from 'react'
import {
  ComposedChart, Area, Line, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip as RechartsTooltip, Legend, Brush, ReferenceArea,
} from 'recharts'
import { useChartTheme } from '@/lib/useChartTheme'
import { useTranslation } from 'react-i18next'
import { Activity, BarChart3, TrendingUp, Download, Maximize2 } from '@/components/brand/icons'
import { cn } from '@/lib/utils'
import { MeasuredChartContainer } from './MeasuredChartContainer'

export type ChartType = 'area' | 'line' | 'bar'

export interface ChartSeries {
  key: string
  name: string
  color?: string
  dashed?: boolean
}

interface InteractiveChartProps {
  data: Record<string, any>[]
  xKey: string
  series: ChartSeries[]
  height?: number
  defaultType?: ChartType
  allowedTypes?: ChartType[]
  yFormatter?: (v: number) => string
  /** форматтер подписей оси X (напр. ISO-дата -> dd.mm) */
  xFormatter?: (v: string) => string
  tooltip?: ReactElement
  /** форматтер значений в дефолтном тултипе (если tooltip-элемент не задан) */
  tooltipFormatter?: (value: number, name: string) => [React.ReactNode, React.ReactNode] | React.ReactNode
  /** форматтер имени серии в легенде */
  legendFormatter?: (name: string) => string
  brush?: boolean
  /** стек серий (area/bar) — для «трафик по нодам» и т.п. */
  stacked?: boolean
  exportName?: string
  className?: string
  /** ключ с «сырым» значением X (напр. ISO-дата) — для onRangeSelect/перезапроса */
  rawKey?: string
  /** протянул интервал на графике -> сюда прилетают границы (raw, если есть rawKey) */
  onRangeSelect?: (from: string, to: string) => void
}

const TYPE_ICONS: Record<ChartType, typeof Activity> = {
  area: TrendingUp,
  line: Activity,
  bar: BarChart3,
}

function toCsv(rows: Record<string, any>[], xKey: string, series: ChartSeries[]): string {
  const cols = [xKey, ...series.map((s) => s.key)]
  const header = [xKey, ...series.map((s) => s.name)].join(',')
  const escape = (v: unknown) => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = rows.map((r) => cols.map((c) => escape(r[c])).join(','))
  return [header, ...lines].join('\n')
}

export function InteractiveChart({
  data, xKey, series, height = 260, defaultType = 'area',
  allowedTypes = ['area', 'line', 'bar'], yFormatter, xFormatter, tooltip, tooltipFormatter, legendFormatter,
  brush, stacked, exportName, className, rawKey, onRangeSelect,
}: InteractiveChartProps) {
  const chart = useChartTheme()
  const { t } = useTranslation()
  const [type, setType] = useState<ChartType>(defaultType)
  const gradId = useRef(`icg-${Math.round(performance.now())}-${Math.random().toString(36).slice(2, 7)}`)

  // ── drag-select интервала прямо на графике (как в Grafana) ──────
  const [dragA, setDragA] = useState<string | null>(null)
  const [dragB, setDragB] = useState<string | null>(null)
  const [zoom, setZoom] = useState<[number, number] | null>(null)

  // при смене данных (напр. родитель перезапросил) — сбрасываем зум
  useEffect(() => { setZoom(null); setDragA(null); setDragB(null) }, [data])

  const viewData = useMemo(
    () => (zoom ? data.slice(zoom[0], zoom[1] + 1) : data),
    [data, zoom],
  )

  const applyDrag = () => {
    if (dragA != null && dragB != null && dragA !== dragB) {
      const iA = data.findIndex((d) => String(d[xKey]) === dragA)
      const iB = data.findIndex((d) => String(d[xKey]) === dragB)
      if (iA >= 0 && iB >= 0) {
        const lo = Math.min(iA, iB)
        const hi = Math.max(iA, iB)
        if (hi > lo) {
          setZoom([lo, hi])
          if (onRangeSelect) {
            const pick = (i: number) => String((rawKey && data[i]?.[rawKey]) ?? data[i]?.[xKey] ?? '')
            onRangeSelect(pick(lo), pick(hi))
          }
        }
      }
    }
    setDragA(null); setDragB(null)
  }

  const colorOf = (s: ChartSeries, i: number) =>
    s.color || (i === 0 ? chart.accentColor : ['#8b5cf6', '#f59e0b', '#10b981', '#ef4444'][(i - 1) % 4])

  const exportCsv = () => {
    const blob = new Blob([toCsv(data, xKey, series)], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${exportName || 'chart'}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const renderSeries = useMemo(() => series.map((s, i) => {
    const color = colorOf(s, i)
    const stackId = stacked ? 'stack' : undefined
    if (type === 'bar') {
      return <Bar key={s.key} dataKey={s.key} name={s.name} fill={color} stackId={stackId}
        radius={stacked ? undefined : [3, 3, 0, 0]} fillOpacity={s.dashed ? 0.4 : 0.85} />
    }
    if (type === 'line') {
      return <Line key={s.key} type="monotone" dataKey={s.key} name={s.name} stroke={color}
        strokeWidth={s.dashed ? 1.5 : 2} strokeDasharray={s.dashed ? '5 5' : undefined}
        dot={false} activeDot={{ r: 4, fill: color }} />
    }
    // area
    return <Area key={s.key} type="monotone" dataKey={s.key} name={s.name} stroke={color} stackId={stackId}
      strokeWidth={s.dashed ? 1.5 : 2} strokeDasharray={s.dashed ? '5 5' : undefined}
      fill={s.dashed ? 'none' : `url(#${gradId.current}-${i})`} dot={false}
      activeDot={{ r: 4, fill: color }} />
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [series, type, stacked, chart.accentColor])

  return (
    <div className={className}>
      {/* тулбар: сброс зума + вид графика + экспорт */}
      <div className="flex items-center justify-end gap-1 mb-1.5">
        {zoom && (
          <button type="button" onClick={() => setZoom(null)} title={t('charts.resetZoom')}
            className="px-2 py-1 rounded-lg border border-primary-500/40 text-primary-300 hover:bg-primary-500/10 transition-colors flex items-center gap-1 text-[11px]">
            <Maximize2 className="w-3.5 h-3.5" /> {t('charts.resetZoom')}
          </button>
        )}
        <div className="flex items-center rounded-lg border border-[var(--glass-border)] overflow-hidden">
          {allowedTypes.map((tp) => {
            const Icon = TYPE_ICONS[tp]
            return (
              <button key={tp} type="button" onClick={() => setType(tp)}
                title={t(`charts.type.${tp}`)}
                className={cn('px-2 py-1 transition-colors', type === tp
                  ? 'bg-primary-500/20 text-primary-300' : 'text-muted-foreground hover:text-white')}>
                <Icon className="w-3.5 h-3.5" />
              </button>
            )
          })}
        </div>
        {exportName && (
          <button type="button" onClick={exportCsv} title={t('charts.exportCsv')}
            className="px-2 py-1 rounded-lg border border-[var(--glass-border)] text-muted-foreground hover:text-white transition-colors">
            <Download className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      <div style={{ height, userSelect: dragA ? 'none' : undefined }}>
        <MeasuredChartContainer width="100%" height="100%">
          <ComposedChart data={viewData} margin={{ top: 5, right: 8, bottom: 0, left: 0 }}
            onMouseDown={(e) => { const l = e?.activeLabel; if (l != null) { setDragA(String(l)); setDragB(String(l)) } }}
            onMouseMove={(e) => { const l = e?.activeLabel; if (dragA && l != null) setDragB(String(l)) }}
            onMouseUp={applyDrag}
            onMouseLeave={() => { if (dragA) applyDrag() }}>
            <defs>
              {series.map((s, i) => (
                <linearGradient key={s.key} id={`${gradId.current}-${i}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={colorOf(s, i)} stopOpacity={0.3} />
                  <stop offset="95%" stopColor={colorOf(s, i)} stopOpacity={0} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} vertical={false} />
            <XAxis dataKey={xKey} tick={{ fill: chart.tick, fontSize: 11 }} axisLine={false} tickLine={false}
              tickFormatter={xFormatter ? (v: string) => xFormatter(v) : undefined} />
            <YAxis tick={{ fill: chart.tick, fontSize: 11 }} axisLine={false} tickLine={false} width={50}
              tickFormatter={yFormatter ? (v: number) => yFormatter(v) : undefined} />
            <RechartsTooltip
              content={tooltip}
              contentStyle={tooltip ? undefined : chart.tooltipStyle}
              formatter={tooltip ? undefined : (tooltipFormatter as never)}
              cursor={{ stroke: `${chart.accentColor}4D` }}
            />
            {series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} formatter={legendFormatter} />}
            {renderSeries}
            {/* подсветка протягиваемого интервала */}
            {dragA && dragB && dragA !== dragB && (
              <ReferenceArea x1={dragA} x2={dragB} strokeOpacity={0}
                fill={chart.accentColor} fillOpacity={0.12} />
            )}
            {brush && !zoom && viewData.length > 8 && (
              <Brush dataKey={xKey} height={18} travellerWidth={8}
                stroke={chart.accentColor} fill="transparent"
                tickFormatter={() => ''} />
            )}
          </ComposedChart>
        </MeasuredChartContainer>
      </div>
      <p className="text-[10px] text-muted-foreground text-center mt-1">{t('charts.dragHint')}</p>
    </div>
  )
}
