// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { STORAGE_KEYS } from '../storageKeys'
import { DEFAULT_SLIDE_LIST_STATE, loadSlideListState, saveSlideListState } from './slideListState'

afterEach(() => sessionStorage.clear())

describe('slide list state', () => {
  it('starts from the defaults when nothing is saved', () => {
    expect(loadSlideListState()).toEqual(DEFAULT_SLIDE_LIST_STATE)
  })

  it('round-trips search, sort, filters and the panel state', () => {
    const state = {
      searchQ: 'liver',
      sortField: 'title' as const,
      sortOrder: 'desc' as const,
      attrFilters: { stain: new Set(['HE', 'PAS']), organ: new Set(['Liver']) },
      vendorFilter: new Set(['aperio']),
      toursFilter: 'has' as const,
      filtersOpen: true,
    }
    saveSlideListState(state)
    expect(loadSlideListState()).toEqual(state)
  })

  it('does not save an emptied category', () => {
    saveSlideListState({ ...DEFAULT_SLIDE_LIST_STATE, attrFilters: { stain: new Set() } })
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEYS.adminSlideListState)!).attrFilters).toEqual({})
  })

  it('falls back per field on a malformed value, keeping the rest', () => {
    sessionStorage.setItem(STORAGE_KEYS.adminSlideListState, JSON.stringify({
      searchQ: 3,
      sortField: 'bogus',
      sortOrder: 'sideways',
      // Only the categories the panel shows are kept; non-string and empty
      // values are dropped.
      attrFilters: { stain: ['HE', 7, ''], secret: ['x'] },
      vendorFilter: 'aperio',
      toursFilter: 'none',
      filtersOpen: 'yes',
    }))
    expect(loadSlideListState()).toEqual({
      ...DEFAULT_SLIDE_LIST_STATE,
      attrFilters: { stain: new Set(['HE']) },
      toursFilter: 'none',
    })
  })

  it('ignores a value that is not a JSON object', () => {
    for (const raw of ['not json', '[]', 'null']) {
      sessionStorage.setItem(STORAGE_KEYS.adminSlideListState, raw)
      expect(loadSlideListState()).toEqual(DEFAULT_SLIDE_LIST_STATE)
    }
  })
})
