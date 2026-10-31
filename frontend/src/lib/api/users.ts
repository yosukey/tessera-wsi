import { adminFetch, adminGet, adminSend } from './shared'
import type { Role, RolePermissions } from './auth'

export type ManagedUser = {
  id: number
  username: string
  role: Role
  is_active: boolean
  mfa_enabled: boolean
  created_at: string
}

export async function adminListUsers(opts: { signal?: AbortSignal } = {}): Promise<ManagedUser[]> {
  return adminGet('/api/admin/users', 'list users failed', { signal: opts.signal, cache: 'no-store' })
}

export async function adminCreateUser(username: string, password: string, role: Role): Promise<ManagedUser> {
  return adminSend('POST', '/api/admin/users', { username, password, role }, 'create user failed')
}

export async function adminSetUserActive(id: number, isActive: boolean): Promise<ManagedUser> {
  return adminSend('PATCH', `/api/admin/users/${id}`, { is_active: isActive }, 'update user failed')
}

// Turn another member's two-factor sign-in off (lost device). `currentPassword`
// is the acting admin's own.
export async function adminResetUserMfa(id: number, currentPassword: string): Promise<ManagedUser> {
  return adminSend('POST', `/api/admin/users/${id}/mfa/reset`, { current_password: currentPassword }, 'reset mfa failed')
}

export async function adminDeleteUser(id: number): Promise<void> {
  await adminFetch(`/api/admin/users/${id}`, 'delete user failed', { method: 'DELETE' })
}

export async function adminGetRolePermissions(opts: { signal?: AbortSignal } = {}): Promise<RolePermissions> {
  return adminGet(
    '/api/admin/role-permissions',
    'get role permissions failed',
    { signal: opts.signal, cache: 'no-store' },
  )
}

export async function adminUpdateRolePermissions(partial: Partial<RolePermissions>): Promise<RolePermissions> {
  return adminSend('PATCH', '/api/admin/role-permissions', partial, 'update role permissions failed')
}
