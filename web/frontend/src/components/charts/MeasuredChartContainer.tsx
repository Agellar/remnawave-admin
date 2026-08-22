import { ResponsiveContainer, type ResponsiveContainerProps } from 'recharts'

/**
 * Recharts 3 starts ResponsiveContainer at -1x-1 while ResizeObserver has not
 * measured it yet. That expected first frame produces a misleading console
 * warning for every chart. Start with a non-renderable, non-warning sentinel
 * instead: the chart child still waits for a real positive width, exactly as
 * it did before, and ResizeObserver remains the source of truth.
 */
export function MeasuredChartContainer({
  height,
  initialDimension,
  ...props
}: ResponsiveContainerProps) {
  const measuredHeight = typeof height === 'number' && height > 0 ? height : 1

  return (
    <ResponsiveContainer
      {...props}
      height={height}
      initialDimension={initialDimension ?? { width: 0, height: measuredHeight }}
    />
  )
}
