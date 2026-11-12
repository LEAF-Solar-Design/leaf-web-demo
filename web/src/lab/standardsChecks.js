// Test-time checks shared by Lab views; display vocabulary belongs to STD-1.
import glossary from './contract/sources/leaf-glossary.v1.json'

export const VIEW_STATES = Object.freeze(['loading', 'value', 'empty', 'error', 'retry'])

const escapeRegExp = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const replacedWords = [...new Set(glossary.terms.flatMap(term => term.replaces))].sort()
const wordChecks = replacedWords.map(word => [word, new RegExp(`(?<!\\w)${escapeRegExp(word)}(?!\\w)`, 'i')])

export function replacedWordsIn(text, { identifiers = [] } = {}) {
  for (const identifier of identifiers) text = text.split(identifier).join('')
  return wordChecks.filter(([, pattern]) => pattern.test(text)).map(([word]) => word)
}

export function colorLiteralsIn(source) {
  return source.match(/(?<![\w#])#(?:[\da-f]{8}|[\da-f]{6}|[\da-f]{4}|[\da-f]{3})\b|\b(?:rgba?|hsla?)\(/gi) || []
}
