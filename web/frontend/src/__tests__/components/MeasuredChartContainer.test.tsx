import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { MeasuredChartContainer } from '@/components/charts/MeasuredChartContainer'

describe('MeasuredChartContainer', () => {
  it('waits for a real width without Recharts transient-size warnings', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      width: 640,
      height: 240,
      top: 0,
      right: 640,
      bottom: 240,
      left: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    })

    const { container } = render(
      <MeasuredChartContainer width="100%" height="100%">
        <span>chart</span>
      </MeasuredChartContainer>,
    )

    expect(container.querySelector('.recharts-responsive-container')).toBeInTheDocument()
    expect(screen.getByText('chart')).toBeInTheDocument()
    expect(
      warn.mock.calls.some(([message]) => String(message).includes('chart should be greater than 0')),
    ).toBe(false)

    rect.mockRestore()
    warn.mockRestore()
  })
})
