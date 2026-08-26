import client from '@/api/client'
import type {
  IncidentItem,
  IncidentList,
  IncidentReviewLabel,
  IncidentWorkflowStatus,
  QualityItem,
  OperationalContext,
  SystemFreshness,
} from './types'

const BASE = '/plugins/incident_center'

export async function fetchIncidents(active?: boolean): Promise<IncidentList> {
  const { data } = await client.get<IncidentList>(`${BASE}/incidents`, {
    params: { active, limit: 200 },
  })
  return data
}

export async function fetchFreshness(): Promise<SystemFreshness> {
  const { data } = await client.get<SystemFreshness>(`${BASE}/freshness`)
  return data
}

export async function fetchQuality(): Promise<QualityItem[]> {
  const { data } = await client.get<{ items: QualityItem[] }>(`${BASE}/quality`)
  return data.items
}

export async function fetchOperations(): Promise<OperationalContext> {
  const { data } = await client.get<OperationalContext>(`${BASE}/operations`)
  return data
}

export async function updateWorkflow(
  id: number,
  status: IncidentWorkflowStatus,
  snoozeMinutes?: number,
): Promise<IncidentItem> {
  const { data } = await client.put<IncidentItem>(`${BASE}/incidents/${id}/workflow`, {
    status,
    snooze_minutes: snoozeMinutes,
  })
  return data
}

export async function reviewIncident(
  id: number,
  label: IncidentReviewLabel,
): Promise<IncidentItem> {
  const { data } = await client.post<IncidentItem>(`${BASE}/incidents/${id}/review`, { label })
  return data
}
