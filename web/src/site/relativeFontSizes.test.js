// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const expectedSizes = [
  ['../campaigns/campaigns.css', '.campaign-panel h2', 'title'],
  ['../campaigns/campaigns.css', '.campaign-panel .campaign-outputs li > span', 'body'],
  ['../components/CostTransparencyPanel.css', '.cost-footnote', 'body'],
  ['../components/ArloProposalReview.css', '.arlo-geometry figcaption', 'caption'],
  ['../components/ArloProposalReview.css', '.arlo-review pre', 'caption'],
  ['../solar/solarPresetForm.css', '.solar-preset-form .solar-preset-note', 'caption'],
]
const sizeToken = /^var\(--leaf-size-(caption|dense|body|heading|subtitle|title|display)\)$/
const sheets = [...new Set(expectedSizes.map(([file]) => file))].map((file) => {
  const css = readFileSync(new URL(file, import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
  // Match leaf rules so declarations inside media blocks are included too.
  const declarations = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .flatMap(([, selector, body]) => [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)/g)]
      .map(([, property, value]) => ({
        selector: selector.trim(),
        property: property.toLowerCase(),
        value: value.trim(),
      })))
  return { file, declarations }
})

describe('relative font sizes use the v1 type scale', () => {
  for (const [file, selector, size] of expectedSizes) {
    it(`${file} ${selector} uses the ${size} size`, () => {
      const { declarations } = sheets.find((sheet) => sheet.file === file)
      expect(declarations
        .filter((declaration) => declaration.selector === selector && declaration.property === 'font-size')
        .map(({ value }) => value)).toEqual([`var(--leaf-size-${size})`])
    })
  }

  for (const { file, declarations } of sheets) {
    it(`${file} uses only scale tokens for font-size`, () => {
      const sizes = declarations.filter(({ property }) => property === 'font-size')
      expect(sizes.length, file).toBeGreaterThan(0)
      for (const { selector, value } of sizes) {
        expect(value, `${file} ${selector} font-size: ${value}`).toMatch(sizeToken)
      }
    })

    it(`${file} uses inherit or a size token in every font shorthand`, () => {
      for (const { selector, property, value } of declarations) {
        if (property !== 'font') continue
        expect(value, `${file} ${selector} font: ${value}`)
          .toMatch(/^inherit$|var\(--leaf-size-(caption|dense|body|heading|subtitle|title|display)\)/)
      }
    })
  }
})
