import api from '../utils/api'

export interface OrgUnit { code: string; parent_code: string | null; name: string; kind: string; active: boolean }

export async function getOrganizationTree(): Promise<OrgUnit[]> {
  return (await api.get('/organization/tree')).data
}

export async function getOrganizationUnit(code: string) {
  return (await api.get(`/organization/units/${encodeURIComponent(code)}`)).data
}

export async function previewOrganizationUser(userId: number) {
  return (await api.get(`/organization/preview/${userId}`)).data
}

export async function updateOrganizationPerson(personCode: string, payload: Record<string, unknown>) {
  return (await api.patch(`/organization/people/${encodeURIComponent(personCode)}`, payload)).data
}

export async function getOrganizationAudit() {
  return (await api.get('/organization/audit')).data
}
