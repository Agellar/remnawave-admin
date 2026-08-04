/**
 * API-клиент плагина retention-radar.
 *
 * Axios панели (``@/api/client``) уже настроен на базовый ``/api/v2``,
 * поэтому пути начинаются с ``/plugins/retention_radar`` — ровно так их
 * монтирует загрузчик плагинов.
 */
import client from '@/api/client'

import type {
  CampaignPreview,
  CampaignRecord,
  CampaignResult,
  OverviewResponse,
  SegmentKey,
  SegmentResponse,
  ThresholdSettings,
} from './types'

export { asLicenseError } from '@/components/plugins/license'

const BASE = '/plugins/retention_radar'

export async function fetchOverview(): Promise<OverviewResponse> {
  const { data } = await client.get<OverviewResponse>(`${BASE}/overview`)
  return data
}

export async function fetchSegment(
  key: SegmentKey,
  limit = 50,
  offset = 0,
): Promise<SegmentResponse> {
  const { data } = await client.get<SegmentResponse>(`${BASE}/segment/${key}`, {
    params: { limit, offset },
  })
  return data
}

export async function fetchSettings(): Promise<ThresholdSettings> {
  const { data } = await client.get<ThresholdSettings>(`${BASE}/settings`)
  return data
}

export async function saveSettings(
  values: Partial<ThresholdSettings>,
): Promise<ThresholdSettings> {
  const { data } = await client.put<ThresholdSettings>(`${BASE}/settings`, { values })
  return data
}

/**
 * Скачивание CSV. Идём через axios с ``responseType: 'blob'``, а не
 * прямой ссылкой: экспорт закрыт правом retention_radar.export, и токен
 * живёт в заголовке, которого у обычного <a href> нет.
 */
export async function downloadSegmentCsv(key: SegmentKey): Promise<void> {
  const response = await client.get(`${BASE}/segment/${key}/export`, {
    responseType: 'blob',
  })
  const url = window.URL.createObjectURL(new Blob([response.data]))
  const link = document.createElement('a')
  link.href = url
  link.download = `retention-${key}.csv`
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

export async function previewCampaign(
  key: SegmentKey,
  messageText?: string,
): Promise<CampaignPreview> {
  const { data } = await client.post<CampaignPreview>(`${BASE}/campaign/preview`, {
    segment: key,
    message_text: messageText ?? null,
  })
  return data
}

export async function sendCampaign(payload: {
  segment: SegmentKey
  message_text: string
  confirm_token: string
  dry_run: boolean
}): Promise<CampaignResult> {
  const { data } = await client.post<CampaignResult>(`${BASE}/campaign/send`, payload)
  return data
}

export async function fetchCampaigns(limit = 20): Promise<{ items: CampaignRecord[] }> {
  const { data } = await client.get<{ items: CampaignRecord[] }>(`${BASE}/campaigns`, {
    params: { limit },
  })
  return data
}
