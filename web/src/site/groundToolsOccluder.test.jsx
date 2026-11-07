import { describe, expect, it } from 'vitest'
import SurfaceGrounds, { measureContainedWindow } from './SurfaceGrounds.jsx'

const TOOLS_OCCLUDER = ['#project-workspace-tools', 'left']

describe('non-drawing ground tools occluder', () => {
  it('appends the left tools column to every non-drawing ground', () => {
    const occluders = Object.freeze([
      Object.freeze(['header.top', 'top']),
      Object.freeze(['.rail-stack', 'nearest']),
    ])
    const grounds = SurfaceGrounds({ surface: 'browser', occluders }).props.children

    for (const ground of grounds) {
      expect(ground.props.occluders).toEqual([...occluders, TOOLS_OCCLUDER])
      expect(ground.props.occluders).toHaveLength(occluders.length + 1)
    }
  })

  it('retains the derived occluder array when the input array is unchanged', () => {
    const occluders = Object.freeze([Object.freeze(['.rail-stack', 'nearest'])])
    const first = SurfaceGrounds({ surface: 'browser', occluders }).props.children
    const second = SurfaceGrounds({ surface: 'lab', occluders }).props.children

    for (let index = 0; index < first.length; index += 1) {
      expect(second[index].props.occluders).toBe(first[index].props.occluders)
    }
  })

  it('keeps contained windows between the tools column and activity rail', () => {
    const rect = ({ left, top, width, height }) => ({
      left, top, width, height, right: left + width, bottom: top + height,
    })
    const board = { getBoundingClientRect: () => rect({ left: 0, top: 0, width: 1440, height: 900 }) }
    const elements = {
      'header.top': { getBoundingClientRect: () => rect({ left: 0, top: 0, width: 1440, height: 28 }) },
      '#project-workspace-tools': { getBoundingClientRect: () => rect({ left: 0, top: 28, width: 300, height: 841 }) },
      '.rail-stack': { getBoundingClientRect: () => rect({ left: 1140, top: 28, width: 300, height: 841 }) },
    }
    const doc = { querySelector: (selector) => elements[selector] }
    const occluders = [
      ['header.top', 'top'],
      ['.rail-stack', 'nearest'],
      TOOLS_OCCLUDER,
    ]

    const measured = measureContainedWindow(board, doc, occluders)
    expect(measured.left).toBe(316)
    expect(measured.left + measured.width).toBeLessThanOrEqual(1124)

    elements['#project-workspace-tools'].getBoundingClientRect = () => rect({ left: 0, top: 28, width: 0, height: 841 })
    expect(measureContainedWindow(board, doc, occluders).left).toBe(16)
  })
})
