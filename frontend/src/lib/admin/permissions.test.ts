import { describe, expect, it } from 'vitest'
import type { RolePermissions, Role } from '../api'
import { canViewAnalytics, canViewServer, isAdmin } from './permissions'

const none: RolePermissions = {
  manager_can_view_analytics: false,
  manager_can_view_server: false,
  instructor_can_view_analytics: false,
  instructor_can_view_server: false,
}
const me = (role: Role, grants: Partial<RolePermissions> = {}) => ({
  role,
  role_permissions: { ...none, ...grants },
})

describe('staff page permissions', () => {
  it('lets an admin see everything, whatever the flags say', () => {
    expect(canViewAnalytics(me('admin'))).toBe(true)
    expect(canViewServer(me('admin'))).toBe(true)
    expect(isAdmin(me('admin'))).toBe(true)
  })

  it("follows each role's own flag for managers and instructors", () => {
    expect(canViewAnalytics(me('manager'))).toBe(false)
    expect(canViewAnalytics(me('manager', { manager_can_view_analytics: true }))).toBe(true)
    expect(canViewAnalytics(me('manager', { instructor_can_view_analytics: true }))).toBe(false)
    expect(canViewServer(me('manager', { manager_can_view_server: true }))).toBe(true)
    expect(canViewServer(me('manager', { manager_can_view_analytics: true }))).toBe(false)

    expect(canViewAnalytics(me('instructor', { instructor_can_view_analytics: true }))).toBe(true)
    expect(canViewAnalytics(me('instructor', { manager_can_view_analytics: true }))).toBe(false)
    expect(canViewServer(me('instructor', { instructor_can_view_server: true }))).toBe(true)
    expect(canViewServer(me('instructor', { manager_can_view_server: true }))).toBe(false)
  })

  it('keeps admin-only pages to admins', () => {
    const all: Partial<RolePermissions> = {
      manager_can_view_analytics: true,
      manager_can_view_server: true,
      instructor_can_view_analytics: true,
      instructor_can_view_server: true,
    }
    expect(isAdmin(me('manager', all))).toBe(false)
    expect(isAdmin(me('instructor', all))).toBe(false)
  })
})
