// Which staff pages a signed-in user may open, from /api/admin/me. The server
// enforces the same rules on every endpoint; these only decide what the UI
// offers and when a page sends its visitor back to the dashboard.
import type { AdminMe } from '../api'

type Me = Pick<AdminMe, 'role' | 'role_permissions'>

// Admins always; managers and instructors when the admin-set role
// permissions grant it.
export function canViewAnalytics(me: Me): boolean {
  return me.role === 'admin'
    || (me.role === 'manager' && me.role_permissions.manager_can_view_analytics)
    || (me.role === 'instructor' && me.role_permissions.instructor_can_view_analytics)
}

// The Server Info page and the Performance page behind it: admins always; managers
// and instructors when the role permissions grant it.
export function canViewServer(me: Me): boolean {
  return me.role === 'admin'
    || (me.role === 'manager' && me.role_permissions.manager_can_view_server)
    || (me.role === 'instructor' && me.role_permissions.instructor_can_view_server)
}

// Pages with no role-permission opt-in (audit history, error logs).
export function isAdmin(me: Pick<AdminMe, 'role'>): boolean {
  return me.role === 'admin'
}
