import {describe,it,expect} from 'vitest'
import {DuplicateOriginalMember,parseProjectOriginalJson} from './projectOriginalJson.js'
describe('unambiguous original JSON',()=>{
 it.each([
  '{"account":"first","account":"second"}',
  '{"rows":[{"pair_key":"a","pair_key":"b"}]}',
  '{"rows":[{"remaining_pct":80,"remaining_pct":1}]}',
  String.raw`{"account":"a","acc\u006funt":"b"}`,
  '{"__proto__":1,"__proto__":2}',
 ])('refuses ambiguous decoded member names in %s',raw=>{
  expect(()=>parseProjectOriginalJson(raw)).toThrow(DuplicateOriginalMember)
 })
 it('keeps sibling scopes, prototype-like names, escaped strings and Unicode unchanged',()=>{
  const raw=String.raw`{"rows":[{"account":"one","note":"fake \"key\":{}"},{"account":"two"}],"constructor":"literal","__proto__":{"safe":true},"חساب":"قيمة"}`
  const value=parseProjectOriginalJson(raw)
  expect(value).toEqual(JSON.parse(raw));expect(Object.getPrototypeOf(value)).toBe(Object.prototype)
  expect(Object.prototype).not.toHaveProperty('safe')
 })
 it.each(['{"account":1,}', '{"account":NaN}', '{"unterminated":"x'])('keeps native invalid JSON refusal for %s',raw=>{
  expect(()=>parseProjectOriginalJson(raw)).toThrow(SyntaxError)
 })
 it('bounds scanner stack before unsupported deep metadata can allocate per-object state',()=>{
  expect(()=>parseProjectOriginalJson('['.repeat(65)+'0'+']'.repeat(65))).toThrow('nesting is too deep')
  expect(parseProjectOriginalJson('['.repeat(32)+'0'+']'.repeat(32))).toEqual(JSON.parse('['.repeat(32)+'0'+']'.repeat(32)))
 })
})
